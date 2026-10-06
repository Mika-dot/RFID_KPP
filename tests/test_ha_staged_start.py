"""A rollout start must require the pinned passive maintenance state."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("staged_start", Path(__file__).parents[1] /
                                           "deploy/ha/start_staged_rollout.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class StagedStartTests(unittest.TestCase):
    def test_staged_identity_refuses_active_unpaused_old_or_stale_agents(self):
        good = {"node": "physical", "release_sha": app.RELEASE, "fencing_protocol": 2,
                "sample_age": 1, "active": False, "operator_maintenance": True}
        self.assertTrue(app.staged(200, good, "physical"))
        for change in ({"active": True}, {"operator_maintenance": False}, {"fencing_protocol": 1},
                       {"sample_age": 12}, {"release_sha": "b" * 40}, {"node": "perimetr"},
                       {"resources": {"restart_required": True}}):
            self.assertFalse(app.staged(200, {**good, **change}, "physical"))
        self.assertFalse(app.staged(401, good, "physical"))

    def test_record_requires_durable_maintenance_and_independent_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            record = {"current": {"sha": app.RELEASE, "root": str(root)}, "fencing_protocol_min": 2,
                      "pending": False, "previous": None}
            (root / "release.json").write_text(json.dumps(record))
            (root / "operator-maintenance.json").write_text('{"enabled":true}')
            (root / "repair-verification.json").write_text('{"required":true}')
            cfg = {"state_dir": str(root)}
            app.check_record(cfg, root)
            for filename, text in (("operator-maintenance.json", '{"enabled":false}'),
                                   ("repair-verification.json", '{"required":false}')):
                path = root / filename
                previous = path.read_text()
                path.write_text(text)
                with self.assertRaises(app.Abort):
                    app.check_record(cfg, root)
                path.write_text(previous)
            for change in ({"pending": True}, {"fencing_protocol_min": 1},
                           {"current": {"sha": "a" * 40, "root": str(root)}}):
                (root / "release.json").write_text(json.dumps({**record, **change}))
                with self.assertRaises(app.Abort):
                    app.check_record(cfg, root)

    def test_summary_never_forwards_logs_worker_argv_or_environment(self):
        result = app.summary({"node": "physical", "logs": "private-log", "env": {"PWD": "secret"},
                              "workers": {"command": "private-command"}})
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
