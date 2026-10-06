"""Cutover readiness and rollback interlocks; never use real production data."""
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location("protocol2_cutover", Path(__file__).parents[1] /
                                           "deploy/ha/cutover_protocol2.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class Protocol2CutoverTests(unittest.TestCase):
    def test_capture_accepts_mixed_case_cmd_names_and_rejects_missing_or_duplicate_launcher(self):
        wrappers = {"RUN_RFID_READER_V3.cmd": "RFID_reader_v4", "RUN_RUSGUARD_V3.cmd": "DB_RusGard",
                    "RUN_RTSP_V3.cmd": "RTSP", "RUN_AGGREGATOR_V3.cmd": "KPP", "RUN_WEB_V3.cmd": "web"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy").mkdir()
            processes = []
            for i, (name, cwd) in enumerate(wrappers.items()):
                (root / "deploy" / name).touch()
                (root / cwd).mkdir()
                p = MagicMock()
                p.pid = i + 100
                p.name.return_value = "cmd.exe"
                p.cmdline.return_value = ["cmd.exe", "/D", "/K", "call", str(root / "deploy" / name)]
                p.children.return_value = []
                p.create_time.return_value = 10 + i
                p.cwd.return_value = str(root)
                processes.append(p)
            cutover = app.Cutover.__new__(app.Cutover)
            cutover.legacy, cutover.wrappers = root, wrappers
            cutover.launcher_pattern = re.compile(r"(?i)\bRUN_(?:RFID_READER|RUSGUARD|RTSP|AGGREGATOR|WEB)_V3\.cmd\b")
            cutover.legacy_processes = MagicMock(return_value=processes)
            cutover.backup, cutover.saved = root / "backup", []
            cutover.save_json = MagicMock()
            found = cutover.capture_legacy()
            self.assertEqual(set(found), {name.upper() for name in wrappers})
            self.assertEqual(len(cutover.saved), 5)
            self.assertEqual([row["argv"] for row in cutover.saved], [p.cmdline() for p in processes])
            cutover.save_json.assert_called_once()
            cutover.save_json.reset_mock()
            for candidates in (processes[:-1], processes + [processes[0]]):
                cutover.legacy_processes.return_value = candidates
                with self.assertRaises(app.Abort):
                    cutover.capture_legacy()
                cutover.save_json.assert_not_called()

    def test_cli_success_has_zero_exit_and_failure_does_not_print_secrets(self):
        with patch.object(app, "main", return_value=0), patch("builtins.print") as out:
            self.assertEqual(app.cli(), 0)
            out.assert_not_called()
        with patch.object(app, "main", side_effect=RuntimeError("password=private")), patch("builtins.print") as out:
            self.assertEqual(app.cli(), 2)
            self.assertEqual(out.call_args.args, ("CUTOVER_PROTOCOL2_STOPPED", "RuntimeError"))

    def test_preflight_allows_only_occupied_ports_on_legacy_physical(self):
        checks = {key: True for key in app.CHECKS}
        self.assertTrue(app.preflight_ok({"preflight": {"checks": checks}}))
        physical = {**checks, "ports_free": False}
        self.assertTrue(app.preflight_ok({"preflight": {"checks": physical}}, legacy=True))
        self.assertFalse(app.preflight_ok({"preflight": {"checks": physical}}))
        for key in app.CHECKS - {"ports_free"}:
            self.assertFalse(app.preflight_ok({"preflight": {"checks": {**physical, key: False}}}, legacy=True))
        self.assertFalse(app.preflight_ok({"preflight": {"checks": {"starting": False}}}, legacy=True))

    def test_no_success_without_five_workers_two_reserves_and_same_epoch(self):
        lease = {"enabled": True, "valid": True, "owner": "physical", "epoch": 134}
        rows = {"physical": {"active": True, "healthy": True, "faulted": False, "epoch": 134,
                             "services": {str(i): {"ok": True} for i in range(5)}},
                "perimetr": {"active": False, "prepared": True, "faulted": False},
                "comparator": {"active": False, "prepared": True, "faulted": False}}
        controller = {"owner": "comparator", "valid": True}
        self.assertTrue(app.cluster_healthy(lease, rows, controller))
        self.assertFalse(app.cluster_healthy({**lease, "epoch": 135}, rows, controller))
        self.assertFalse(app.cluster_healthy(lease, rows, {"owner": "perimetr", "valid": True}))
        for node, change in (("physical", {"services": {}}), ("perimetr", {"faulted": True}),
                              ("comparator", {"active": True}), ("perimetr", {"prepared": False})):
            self.assertFalse(app.cluster_healthy(lease, {**rows, node: {**rows[node], **change}}, controller))

    def test_fencing_failure_never_disables_lease_or_commits(self):
        cutover = app.Cutover.__new__(app.Cutover)
        cutover.store = MagicMock()
        cutover.store.lease.return_value = {"enabled": True, "owner": "physical"}
        conn = MagicMock()
        cutover.connect = MagicMock(return_value=conn)
        cutover.barrier = MagicMock(side_effect=RuntimeError("EpochBarrierUnavailable"))
        with self.assertRaises(RuntimeError):
            cutover.fence_off()
        conn.execute.assert_not_called()
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()

    def test_already_off_rollback_does_not_take_conflicting_legacy_lease_lock(self):
        cutover = app.Cutover.__new__(app.Cutover)
        cutover.store = MagicMock()
        cutover.store.lease.return_value = {"enabled": False, "owner": None}
        cutover.connect = MagicMock()
        cutover.fence_off()
        cutover.connect.assert_not_called()

    def test_roll_back_cannot_launch_legacy_until_fenced(self):
        cutover = app.Cutover.__new__(app.Cutover)
        cutover.fence_off = MagicMock(side_effect=RuntimeError("uncertain SQL"))
        cutover.maintenance = MagicMock()
        with self.assertRaises(RuntimeError):
            cutover.rollback()
        cutover.maintenance.assert_not_called()

    def test_operator_interlock_requires_off_and_real_comparator_identity(self):
        cutover = app.Cutover.__new__(app.Cutover)
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [(False, None), ("perimetr", "private-token", True)]
        cutover.connect = MagicMock(return_value=conn)
        cutover.barrier = MagicMock()
        cutover.save_json = MagicMock()
        with self.assertRaises(app.Abort):
            cutover.hold_controller()
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        self.assertTrue(all(c.args[0].startswith("SELECT ") for c in conn.execute.call_args_list))
        self.assertIn("READCOMMITTEDLOCK", conn.execute.call_args_list[0].args[0])

    def test_controller_handoff_updates_only_owned_operator_token(self):
        cutover = app.Cutover.__new__(app.Cutover)
        cutover.old_controller = {"owner": "comparator", "token": "old-private"}
        cutover.operator_token = "operator-private"
        cutover.disabled = MagicMock()
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        cutover.connect = MagicMock(return_value=conn)
        with self.assertRaises(app.Abort):
            cutover.release_interlock(verify=True)
        sql, owner, token, operator = conn.execute.call_args.args
        self.assertIn("WHERE Id=1 AND Token=?", sql)
        self.assertEqual((owner, token, operator), ("comparator", "old-private", "operator-private"))

    def test_failed_migration_restores_legacy_but_failed_pre_stop_interlock_does_not_restart_it(self):
        for fail_before_stop in (False, True):
            with self.subTest(fail_before_stop=fail_before_stop):
                cutover = app.Cutover.__new__(app.Cutover)
                checks = {key: True for key in app.CHECKS}
                cutover.passive = MagicMock(return_value={
                    n: {"preflight": {"checks": {**checks, "ports_free": n != "physical"}}}
                    for n, _ in app.PRIORITY})
                cutover.disabled = MagicMock()
                cutover.store = MagicMock()
                cutover.controller = MagicMock(return_value={"owner": "comparator", "valid": True})
                cutover.services_health = MagicMock(return_value={str(i): {"ok": True} for i in range(5)})
                cutover.driver = MagicMock()
                cutover.driver.connect.return_value.execute.return_value.fetchone.return_value = ("server", "db")
                cutover.migration_login = MagicMock(return_value="private-login")
                cutover.cfg = {"state_dir": "unused-test-state"}
                cutover.private_directory = MagicMock()
                cutover.save_json = MagicMock()
                cutover.phase = MagicMock()
                targets = {str(i): MagicMock() for i in range(5)}
                cutover.saved = [{"wrapper": name, "pid": i, "created": 1,
                                  "argv": p.cmdline.return_value} for i, (name, p) in enumerate(targets.items())]
                cutover.capture_legacy = MagicMock(return_value=targets)
                cutover.hold_controller = MagicMock(side_effect=app.Abort("interlock failure") if fail_before_stop else None)
                cutover.release_interlock = MagicMock()
                cutover.renew_interlock = MagicMock()
                cutover.same_process = MagicMock(return_value=True)
                cutover.stop_tree = MagicMock()
                cutover.legacy_processes = MagicMock(return_value=[])
                cutover.maintenance = MagicMock()
                cutover.migrate = MagicMock(side_effect=app.Abort("migration failure"))
                cutover.enable = MagicMock()
                cutover.rollback = MagicMock()
                with patch.dict(app.os.environ, {"KPP_CONN_STR": "private"}), patch("builtins.print"):
                    with self.assertRaises(app.Abort):
                        cutover.activate()
                cutover.enable.assert_not_called()
                if fail_before_stop:
                    cutover.stop_tree.assert_not_called()
                    cutover.rollback.assert_not_called()
                    cutover.release_interlock.assert_called_once()
                else:
                    self.assertEqual(cutover.stop_tree.call_count, 5)
                    cutover.rollback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
