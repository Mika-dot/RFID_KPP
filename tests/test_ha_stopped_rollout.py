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
        with patch.object(app.subprocess, "run", return_value=r) as run, \
                patch.object(psutil, "process_iter", return_value=[]), \
                patch.object(app, "windows_port_free") as free:
            app.native_stopped(True, Path("HA-source").resolve(), Path("node.json").resolve())
        free.assert_called_once_with(18200)
        command = " ".join(run.call_args.args[0])
        for forbidden in ("Stop-ScheduledTask", "Start-ScheduledTask", "Disable-ScheduledTask", "taskkill"):
            self.assertNotIn(forbidden, command)
        self.assertIn("Get-ScheduledTask", command)

    def test_windows_check_accepts_exclusive_bind_without_connect_or_listen(self):
        import psutil
        sock = MagicMock()
        probe = sock.__enter__.return_value
        with patch.object(psutil, "net_connections", return_value=[]), \
                patch.object(app.socket, "SO_EXCLUSIVEADDRUSE", -5, create=True), \
                patch.object(app.socket, "socket", return_value=sock):
            app.windows_port_free(18200)
        probe.setsockopt.assert_called_once_with(app.socket.SOL_SOCKET, -5, 1)
        probe.bind.assert_called_once_with(("0.0.0.0", 18200))
        probe.connect_ex.assert_not_called()
        probe.listen.assert_not_called()
        sock.__exit__.assert_called_once()

    def test_windows_check_refuses_listener_and_reports_only_pid(self):
        import psutil
        listener = MagicMock(status=psutil.CONN_LISTEN, pid=1234)
        listener.laddr.port = 18200
        with patch.object(psutil, "net_connections", return_value=[listener]), \
                patch.object(app.socket, "socket") as factory:
            with self.assertRaisesRegex(app.Abort, "HA_PORT_LISTENING port=18200 pids=1234"):
                app.windows_port_free(18200)
        factory.assert_not_called()

    def test_windows_check_never_accepts_bind_failure_or_prints_raw_error(self):
        import psutil
        for code in (10013, 10048):
            sock = MagicMock()
            sock.__enter__.return_value.bind.side_effect = OSError(code, "private-error-text")
            with patch.object(psutil, "net_connections", return_value=[]), \
                    patch.object(app.socket, "SO_EXCLUSIVEADDRUSE", -5, create=True), \
                    patch.object(app.socket, "socket", return_value=sock):
                with self.assertRaises(app.Abort) as context:
                    app.windows_port_free(18200)
            self.assertIn("code=" + str(code), str(context.exception))
            self.assertNotIn("private-error-text", str(context.exception))
            sock.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
