"""Guards for upgrading a stopped installation without touching production SQL."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location("stopped_upgrade", Path(__file__).parents[1] /
                                           "deploy/ha/upgrade_stopped_rollout.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class StoppedRolloutTests(unittest.TestCase):
    def test_sql_check_is_select_only_and_closes_without_committing(self):
        driver = MagicMock()
        conn = driver.connect.return_value
        conn.execute.return_value.fetchone.return_value = (False, None)
        app.disabled_database(driver, "private-connection")
        self.assertEqual(len(conn.execute.call_args_list), 1)
        self.assertTrue(conn.execute.call_args.args[0].startswith("SELECT "))
        conn.commit.assert_not_called()
        conn.close.assert_called_once()

    def test_sql_check_refuses_enabled_or_owned_lease(self):
        for row in ((True, None), (False, "physical"), None):
            driver = MagicMock()
            conn = driver.connect.return_value
            conn.execute.return_value.fetchone.return_value = row
            with self.assertRaises(app.Abort):
                app.disabled_database(driver, "private-connection")
            conn.close.assert_called_once()

    def test_interrupted_merge_can_resume_only_with_matching_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            release = "a" * 40
            state = {"current": {"root": str(root), "sha": app.BASE}, "previous": None, "pending": False}
            with self.assertRaises(app.Abort):
                app.valid_record(state, root, release, release, None)
            journal = {"root": str(root), "base": app.BASE, "release": release}
            app.valid_record(state, root, release, release, journal)
            with self.assertRaises(app.Abort):
                app.valid_record(state, root, release, release, {**journal, "release": "b" * 40})

    def test_pending_release_or_unexpected_base_cannot_be_rewritten(self):
        root = Path(".").resolve()
        for head, pending, previous in ((app.BASE, True, None), (app.BASE, False, {}),
                                        ("c" * 40, False, None)):
            state = {"current": {"root": str(root), "sha": head}, "pending": pending, "previous": previous}
            with self.assertRaises(app.Abort):
                app.valid_record(state, root, head, "a" * 40, None)

    def test_disabled_windows_task_still_refuses_orphan_ha_process(self):
        import psutil
        r = MagicMock(returncode=0, stdout=b'{"state":"Disabled","enabled":false}')
        orphan = MagicMock(pid=999999)
        orphan.name.return_value = "python.exe"
        root = Path("HA-source").resolve()
        config = Path("node.json").resolve()
        orphan.cmdline.return_value = ["python.exe", "-m", "guardian", "--config", str(config)]
        with patch.object(app.subprocess, "run", return_value=r) as run, \
                patch.object(psutil, "process_iter", return_value=[orphan]):
            with self.assertRaises(app.Abort):
                app.native_stopped(True, root, config)
        run.assert_called_once()

    def test_stopped_check_uses_no_stop_start_or_disable_command(self):
        import psutil
        r = MagicMock(returncode=0, stdout=b'{"state":"Disabled","enabled":false}')
        sock = MagicMock()
        sock.__enter__.return_value.connect_ex.return_value = 10061
        with patch.object(app.subprocess, "run", return_value=r) as run, \
                patch.object(psutil, "process_iter", return_value=[]), \
                patch.object(app.socket, "socket", return_value=sock):
            app.native_stopped(True, Path("HA-source").resolve(), Path("node.json").resolve())
        command = " ".join(run.call_args.args[0])
        for forbidden in ("Stop-ScheduledTask", "Start-ScheduledTask", "Disable-ScheduledTask", "taskkill"):
            self.assertNotIn(forbidden, command)
        self.assertIn("Get-ScheduledTask", command)


if __name__ == "__main__":
    unittest.main()
