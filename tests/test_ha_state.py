import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from guardian.config import atomic_json


class DurableStateTests(unittest.TestCase):
    def test_concurrent_writers_leave_one_complete_record_and_no_temp_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            records = [{"writer":i, "payload":str(i)*4096} for i in range(32)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda record:atomic_json(path, record), records))
            self.assertIn(json.loads(path.read_text()), records)
            self.assertEqual(["state.json"], [p.name for p in Path(folder).iterdir()])
