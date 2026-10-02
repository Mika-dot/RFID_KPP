import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.single_instance import SingleInstanceLock
from guardian.boot import launch
from guardian.config import atomic_json


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {"root":self.tmp.name, "state_dir":self.tmp.name, "python":"python"}
        self.record = Path(self.tmp.name) / "release.json"
        self.old = {"root":self.tmp.name, "sha":"a"*40, "python":"old-python"}
        self.new = {"root":self.tmp.name, "sha":"b"*40, "python":"new-python"}
        atomic_json(self.record, {"current":self.new, "previous":self.old,
                                "pending":True, "trial_started":False})

    def state(self):
        return json.loads(self.record.read_text())

    @patch("guardian.boot.subprocess.call", return_value=2)
    def test_import_failure_rolls_back_before_agent_can_start(self, call):
        self.assertEqual(75, launch(self.cfg, "node.json"))
        self.assertEqual(self.old, self.state()["current"])
        self.assertFalse(self.state()["pending"])
        self.assertIn("b"*40, self.state()["rejected"])
        self.assertEqual("new-python", call.call_args.args[0][0])

    @patch("guardian.boot.subprocess.call", side_effect=FileNotFoundError)
    def test_missing_candidate_interpreter_also_rolls_back(self, call):
        self.assertEqual(75, launch(self.cfg, "node.json"))
        self.assertEqual("old-python", self.state()["current"]["python"])

    def test_shutdown_and_requested_restart_preserve_pending_release(self):
        for code in (0, 75, -2, -15, 130, 143):
            with self.subTest(code=code), patch("guardian.boot.subprocess.call", return_value=code):
                self.assertEqual(code, launch(self.cfg, "node.json"))
                self.assertEqual(self.new, self.state()["current"])

    @patch("guardian.boot.subprocess.call", return_value=2)
    def test_duplicate_launcher_cannot_roll_back_running_agent(self, call):
        with SingleInstanceLock(str(Path(self.tmp.name) / "guardian.lock")):
            self.assertEqual(2, launch(self.cfg, "node.json"))
        self.assertEqual(self.new, self.state()["current"])

    @patch("guardian.boot.subprocess.call", return_value=2)
    def test_confirmed_release_failure_does_not_rewrite_state(self, call):
        state = self.state()
        state["pending"] = False
        atomic_json(self.record, state)
        self.assertEqual(2, launch(self.cfg, "node.json"))
        self.assertEqual(state, self.state())

    def test_release_changed_by_agent_is_not_overwritten(self):
        def changed(*args, **kwargs):
            state = self.state()
            state["current"] = self.old
            atomic_json(self.record, state)
            return 2
        with patch("guardian.boot.subprocess.call", side_effect=changed):
            self.assertEqual(2, launch(self.cfg, "node.json"))
        self.assertNotIn("rejected", self.state())
