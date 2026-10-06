"""Exercise failure-injection identity guards and automatic recovery, with mocks."""
import copy
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location("failover_acceptance", Path(__file__).parents[1] /
                                            "deploy/ha/test_protocol2_failover.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def snapshot(owner="physical", held=False, epoch=134):
    rows = {n: {"active": n == owner, "healthy": n == owner, "prepared": True, "faulted": False,
                "epoch": epoch, "operator_maintenance": False,
                "services": {s: {"ok": True} for s in app.SERVICES} if n == owner else {}}
            for n in app.NODES}
    if held:
        rows["physical"].update(prepared=False, faulted=True, operator_maintenance=True)
    return {"lease": {"enabled": True, "valid": True, "owner": owner, "epoch": epoch},
            "nodes": rows, "consistent": True, "controller": {"owner": "comparator", "valid": True}}


class FailoverAcceptanceTests(unittest.TestCase):
    def test_only_exact_registered_ha_aggregator_for_current_epoch_is_eligible(self):
        cfg = {"root": "D:/PerimeterHA/source", "python": "D:/legacy/venv64/Scripts/python.exe",
               "state_dir": "D:/PerimeterHA/state"}
        argv = [cfg["python"], "-u", "D:/PerimeterHA/source/deploy/run_service.py", "--service",
                "Perimeter.Aggregator", "--script", "D:/PerimeterHA/source/deploy/monitored_aggregator.py"]
        env = {"PERIMETER_HA_NODE": "physical", "PERIMETER_HA_EPOCH": "134",
               "PERIMETER_HA_STATE_DIR": cfg["state_dir"]}
        lease = snapshot()["lease"]
        self.assertTrue(app.aggregator_identity(argv, cfg["python"], env, cfg, lease))
        self.assertTrue(app.aggregator_identity([v.replace("/", "\\") for v in argv],
                                               cfg["python"].upper(), env, cfg, lease))
        for mutation in ({"enabled": False}, {"valid": False}, {"owner": "perimetr"}, {"epoch": 135}):
            self.assertFalse(app.aggregator_identity(argv, cfg["python"], env, cfg, {**lease, **mutation}))
        for key, value in (("PERIMETER_HA_NODE", "perimetr"), ("PERIMETER_HA_EPOCH", "133"),
                           ("PERIMETER_HA_STATE_DIR", "D:/legacy/state")):
            self.assertFalse(app.aggregator_identity(argv, cfg["python"], {**env, key: value}, cfg, lease))
        for index, replacement in ((2, "D:/legacy/deploy/run_service.py"), (4, "Perimeter.RfidReader"),
                                   (6, "D:/legacy/deploy/monitored_aggregator.py")):
            wrong = list(argv)
            wrong[index] = replacement
            self.assertFalse(app.aggregator_identity(wrong, cfg["python"], env, cfg, lease))
        self.assertFalse(app.aggregator_identity(argv, "D:/other/python.exe", env, cfg, lease))

    def test_takeover_requires_five_live_services_and_exact_epoch_controller(self):
        for owner, held in (("physical", False), ("perimetr", True)):
            original = snapshot(owner, held)
            self.assertTrue(app.ready(original, owner, held))
            for field, value in (("epoch", 135), ("healthy", False), ("prepared", False),
                                 ("faulted", True), ("operator_maintenance", True)):
                s = copy.deepcopy(original)
                s["nodes"][owner][field] = value
                self.assertFalse(app.ready(s, owner, held))
            s = copy.deepcopy(original)
            s["nodes"][owner]["services"]["RfidReader"]["ok"] = False
            self.assertFalse(app.ready(s, owner, held))
            s = copy.deepcopy(original)
            s["controller"]["owner"] = "perimetr"
            self.assertFalse(app.ready(s, owner, held))
            self.assertFalse(app.ready({**original, "consistent": False}, owner, held))
        s = snapshot("perimetr", True)
        s["nodes"]["comparator"]["active"] = True
        self.assertFalse(app.ready(s, "perimetr", True))

    def test_bad_registration_or_epoch_never_kills_any_process(self):
        test = app.Test.__new__(app.Test)
        test.cfg = {"root": "D:/PerimeterHA/source", "python": "D:/python.exe", "state_dir": "D:/state"}
        test.store = MagicMock()
        test.store.lease.return_value = snapshot()["lease"]
        process = MagicMock()
        process.create_time.return_value = 10
        with patch.object(app.subprocess, "run") as kill:
            for created, epoch in ((11, 134), (10, 133)):
                with self.assertRaises(app.Abort):
                    test.stop_aggregator((process, [], created, epoch))
            kill.assert_not_called()

    def test_no_maintenance_request_before_lease_transfer_and_empty_workers(self):
        test = app.Test.__new__(app.Test)
        initial, transferred = snapshot(), snapshot("perimetr")
        test.observe = MagicMock(side_effect=[initial, transferred, transferred])
        test.workers = MagicMock(side_effect=[{"Aggregator": {"running": False}}, {}])
        test.phase, test.maintain = MagicMock(), MagicMock()
        with patch.object(app.time, "sleep"), patch("builtins.print"):
            test.hold_demoted_physical()
        self.assertEqual(test.observe.call_count, 3)
        self.assertEqual(test.workers.call_count, 2)
        test.maintain.assert_called_once_with(True)

    def test_recovery_releases_hold_without_repeatedly_resetting_verification(self):
        test = app.Test.__new__(app.Test)
        test.store = MagicMock()
        test.store.lease.return_value = snapshot("perimetr")["lease"]
        test.status = MagicMock(return_value={"operator_maintenance": True, "faulted": True})
        test.maintain, test.phase = MagicMock(), MagicMock()
        test.release_physical()
        test.maintain.assert_called_once_with(False)
        test.maintain.reset_mock()
        test.status.return_value = {"operator_maintenance": False, "faulted": False}
        test.release_physical()
        test.maintain.assert_not_called()

    def test_external_ha_off_is_not_overridden_during_recovery(self):
        test = app.Test.__new__(app.Test)
        test.store = MagicMock()
        test.store.lease.return_value = {"enabled": False}
        test.maintain = MagicMock()
        with self.assertRaises(app.Halt):
            test.release_physical()
        test.maintain.assert_not_called()

    def test_stale_sample_retries_but_external_mode_change_does_not(self):
        test = app.Test.__new__(app.Test)
        test.observe = MagicMock(side_effect=[app.Abort("stale sample"), snapshot()])
        test.workers = MagicMock(side_effect=[{}, {}, {s: {"running": True} for s in app.SERVICES}])
        test.store = MagicMock()
        test.store.lease.return_value = snapshot()["lease"]
        with patch.object(app.time, "sleep"), patch("builtins.print"):
            self.assertEqual(test.wait_ready("physical", samples=1), snapshot())
        test.observe.side_effect = app.Halt("HA off")
        with self.assertRaises(app.Halt):
            test.wait_ready("physical", samples=1)

    def test_failed_reserve_proof_always_attempts_physical_recovery(self):
        test = app.Test.__new__(app.Test)
        test.cfg = {"state_dir": "unused-test-path"}
        test.precheck, test.private_directory, test.phase = MagicMock(), MagicMock(), MagicMock()
        test.capture_aggregator = MagicMock(return_value=(MagicMock(pid=1), [], 10, 134))
        test.stop_aggregator, test.hold_demoted_physical = MagicMock(), MagicMock()
        test.wait_ready = MagicMock(side_effect=app.Abort("Wine RFID not ready"))
        test.recover = MagicMock()
        with patch("builtins.print"), self.assertRaises(app.Abort):
            test.run()
        test.recover.assert_called_once()
        self.assertFalse(test.record["reserve_proven"])
        self.assertNotIn(("FAILOVER_AND_FAILBACK_PROTOCOL2_OK",), [c.args for c in test.phase.call_args_list])

    def test_role_change_and_bad_sample_reset_the_required_success_streak(self):
        test = app.Test.__new__(app.Test)
        first, second = snapshot(epoch=134), snapshot(epoch=135)
        bad = copy.deepcopy(second)
        bad["nodes"]["physical"]["healthy"] = False
        test.observe = MagicMock(side_effect=[first, second, bad, second, second, second])
        test.workers = MagicMock(side_effect=lambda n: {s: {"running": True} for s in app.SERVICES} if n == "physical" else {})
        test.store = MagicMock()
        test.store.lease.side_effect = [first["lease"], second["lease"], second["lease"], second["lease"], second["lease"]]
        with patch.object(app.time, "sleep"), patch("builtins.print"):
            result = test.wait_ready("physical", samples=3)
        self.assertEqual(test.observe.call_count, 6)
        self.assertEqual(result["lease"]["epoch"], 135)

    def test_failed_capture_does_not_inject_failure_or_request_recovery(self):
        test = app.Test.__new__(app.Test)
        test.precheck = MagicMock()
        test.capture_aggregator = MagicMock(side_effect=app.Abort("wrong process"))
        test.stop_aggregator, test.recover = MagicMock(), MagicMock()
        with self.assertRaises(app.Abort):
            test.run()
        test.stop_aggregator.assert_not_called()
        test.recover.assert_not_called()

    def test_success_cli_exit_zero_and_generic_errors_do_not_print_credentials(self):
        with patch.object(app, "main", return_value=0), patch("builtins.print") as out:
            self.assertEqual(app.cli(), 0)
            out.assert_not_called()
        with patch.object(app, "main", side_effect=RuntimeError("private password")), patch("builtins.print") as out:
            self.assertEqual(app.cli(), 2)
            self.assertEqual(out.call_args.args, ("FAILOVER_TEST_STOPPED", "RuntimeError"))


if __name__ == "__main__":
    unittest.main()
