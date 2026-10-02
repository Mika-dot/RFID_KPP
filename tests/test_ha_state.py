import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from guardian.config import atomic_json


class DurableStateTests(unittest.TestCase):
    def test_temporary_windows_file_sharing_error_retries_atomic_replace(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            atomic_json(path, {"old":True})
            original = os.replace
            blocked = PermissionError("sharing violation")
            blocked.winerror = 32
            calls = []
            def replace(source, destination):
                calls.append(1)
                if len(calls) < 3:
                    raise blocked
                return original(source, destination)
            with patch("guardian.config.os.replace", side_effect=replace), patch("guardian.config.time.sleep"):
                atomic_json(path, {"new":True})
            self.assertEqual({"new":True}, json.loads(path.read_text()))
            self.assertEqual(3, len(calls))

    def test_failed_replace_preserves_previous_complete_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            atomic_json(path, {"old":True})
            with patch("guardian.config.os.replace", side_effect=PermissionError("permanent")):
                with self.assertRaises(PermissionError):
                    atomic_json(path, {"new":True})
            self.assertEqual({"old":True}, json.loads(path.read_text()))
            self.assertEqual(["state.json"], [p.name for p in Path(folder).iterdir()])

    def test_concurrent_writers_leave_one_complete_record_and_no_temp_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            records = [{"writer":i, "payload":str(i)*4096} for i in range(32)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda record:atomic_json(path, record), records))
            self.assertIn(json.loads(path.read_text()), records)
            self.assertEqual(["state.json"], [p.name for p in Path(folder).iterdir()])
