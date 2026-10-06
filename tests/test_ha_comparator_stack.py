import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


base = module("comparator_test_base", "deploy/ha/test_protocol2_failover.py")
app = module("comparator_stack_test", "deploy/ha/test_comparator_stack.py")
Test = app.make_test(base)


def snapshot(owner="physical", held=False, controller="perimetr"):
    rows = {n: {"active": n == owner, "healthy": n == owner, "prepared": True, "faulted": False,
                "epoch": 143, "operator_maintenance": False,
                "services": {s: {"ok": True} for s in base.SERVICES} if n == owner else {}}
            for n in base.NODES}
    if held:
        for n in ("physical", "perimetr"):
            rows[n].update(prepared=False, faulted=True, operator_maintenance=True)
    return {"lease": {"enabled": True, "valid": True, "owner": owner, "epoch": 143},
            "nodes": rows, "consistent": True, "controller": {"owner": controller, "valid": True}}


def test_object():
    t = Test.__new__(Test)
    t.perimetr_held, t.recovering, t.reserve = False, False, "comparator"
    t.record = {"perimetr_held": False}
    return t


class ComparatorStackTests(unittest.TestCase):
    def test_quarantined_comparator_aborts_wait_immediately_but_starting_or_recovery_can_wait(self):
        t = test_object()
        t.perimetr_held = True
        s = snapshot("comparator", held=True)
        s["lease"].update(owner=None, valid=False)
        s["nodes"]["comparator"].update(active=False, faulted=True)
        t.observe = MagicMock(return_value=s)
        with patch.object(base.time, "sleep") as sleep, patch("builtins.print"), self.assertRaises(base.Halt):
            t.wait_ready("comparator", held=True)
        sleep.assert_not_called()
        s["consistent"] = False
        self.assertFalse(t.ready_snapshot(s, "comparator", True))
        s["consistent"] = True
        s["nodes"]["comparator"]["faulted"] = False
        self.assertFalse(t.ready_snapshot(s, "comparator", True))
        s["nodes"]["comparator"]["faulted"] = True
        self.assertFalse(t.ready_snapshot(s, "physical", False))

    def test_requires_actual_perimetr_controller_and_five_comparator_services_with_both_holds(self):
        t = test_object()
        self.assertTrue(t.ready_snapshot(snapshot(), "physical", False))
        self.assertFalse(t.ready_snapshot(snapshot(controller="comparator"), "physical", False))
        t.perimetr_held = True
        s = snapshot("comparator", held=True)
        self.assertTrue(t.ready_snapshot(s, "comparator", True))
        for n in ("physical", "perimetr"):
            bad = copy.deepcopy(s)
            bad["nodes"][n]["operator_maintenance"] = False
            self.assertFalse(t.ready_snapshot(bad, "comparator", True))
        bad = copy.deepcopy(s)
        bad["nodes"]["comparator"]["services"]["RfidReader"]["ok"] = False
        self.assertFalse(t.ready_snapshot(bad, "comparator", True))
        bad = copy.deepcopy(s)
        bad["controller"]["valid"] = False
        self.assertFalse(t.ready_snapshot(bad, "comparator", True))

    def test_hold_intent_is_durable_before_maintenance_and_requires_passive_worker_evidence(self):
        t = test_object()
        t.observe = MagicMock(return_value=snapshot())
        t.workers = MagicMock(return_value={})
        t.store = MagicMock()
        t.store.lease.return_value = snapshot()["lease"]
        calls = []
        t.phase = lambda name: calls.append((name, t.record["perimetr_held"]))
        t.perimetr_maintenance = lambda enabled: calls.append(("API", enabled))
        t.hold_perimetr()
        self.assertEqual(calls[0], ("PERIMETR_EXECUTOR_HOLD_INTENT", True))
        self.assertEqual(calls[1], ("API", True))
        t = test_object()
        t.observe = MagicMock(return_value=snapshot())
        t.workers = MagicMock(return_value={"Aggregator": {"running": True}})
        t.perimetr_maintenance = MagicMock()
        with self.assertRaises(app.Abort):
            t.hold_perimetr()
        t.perimetr_maintenance.assert_not_called()
        self.assertFalse(t.perimetr_held)

    def test_changed_lease_or_wrong_controller_refuses_hold(self):
        for changed_controller in (True, False):
            t = test_object()
            t.observe = MagicMock(return_value=snapshot(controller="comparator" if changed_controller else "perimetr"))
            t.workers = MagicMock(return_value={})
            t.store = MagicMock()
            t.store.lease.return_value = {**snapshot()["lease"], "epoch": 144}
            t.perimetr_maintenance = MagicMock()
            with self.assertRaises(app.Abort):
                t.hold_perimetr()
            t.perimetr_maintenance.assert_not_called()

    def test_lost_release_ack_does_not_reset_independent_verification(self):
        t = test_object()
        t.perimetr_held = True
        t.store = MagicMock()
        t.store.lease.return_value = snapshot("comparator")["lease"]
        t.status = MagicMock(return_value={"operator_maintenance": False, "faulted": True, "active": False})
        t.perimetr_maintenance, t.phase = MagicMock(), MagicMock()
        t.release_perimetr()
        t.perimetr_maintenance.assert_not_called()
        self.assertFalse(t.perimetr_held)

    def test_external_ha_off_refuses_release_without_overriding_sql(self):
        t = test_object()
        t.perimetr_held = True
        t.store = MagicMock()
        t.store.lease.return_value = {"enabled": False}
        t.perimetr_maintenance = MagicMock()
        with self.assertRaises(base.Halt):
            t.release_perimetr()
        t.perimetr_maintenance.assert_not_called()
        t.store.grant.assert_not_called()

    def test_physical_release_failure_still_attempts_perimetr_release(self):
        t = test_object()
        t.release_physical = MagicMock(side_effect=base.Abort("physical unreachable"))
        t.release_perimetr, t.wait_ready = MagicMock(), MagicMock()
        with self.assertRaises(base.Abort):
            t.recover()
        t.release_perimetr.assert_called_once()
        t.wait_ready.assert_not_called()

    def test_failed_comparator_proof_recovers_and_never_reports_test_success(self):
        t = test_object()
        t.cfg = {"state_dir": "unused-comparator-test-path"}
        t.precheck, t.private_directory, t.phase = MagicMock(), MagicMock(), MagicMock()
        t.capture_aggregator = MagicMock(return_value=(MagicMock(pid=1), [], 10, 143))
        t.hold_perimetr, t.stop_aggregator, t.hold_demoted_physical = MagicMock(), MagicMock(), MagicMock()
        order = []
        t.hold_perimetr.side_effect = lambda: order.append("hold_perimetr")
        t.stop_aggregator.side_effect = lambda target: order.append("kill_aggregator")
        t.hold_demoted_physical.side_effect = lambda: order.append("hold_demoted_physical")
        t.wait_ready = MagicMock(side_effect=base.Abort("Comparator SDK not ready"))
        t.recover = MagicMock()
        with patch("builtins.print"), self.assertRaises(base.Abort):
            t.run()
        t.wait_ready.assert_called_once_with("comparator", held=True, timeout=300)
        self.assertEqual(order, ["hold_perimetr", "kill_aggregator", "hold_demoted_physical"])
        t.recover.assert_called_once()
        self.assertFalse(t.record["reserve_proven"])
        self.assertNotIn(("COMPARATOR_STACK_AND_FAILBACK_PROTOCOL2_OK",), [c.args for c in t.phase.call_args_list])

    def test_capture_failure_makes_no_maintenance_or_failure_injection(self):
        t = test_object()
        t.precheck = MagicMock()
        t.capture_aggregator = MagicMock(side_effect=base.Abort("PID changed"))
        t.hold_perimetr, t.stop_aggregator, t.recover = MagicMock(), MagicMock(), MagicMock()
        with self.assertRaises(base.Abort):
            t.run()
        t.hold_perimetr.assert_not_called()
        t.stop_aggregator.assert_not_called()
        t.recover.assert_not_called()

    def test_restored_controller_change_resets_full_stack_success_series(self):
        t = test_object()
        t.recovering = True
        a, b = snapshot(), snapshot(controller="comparator")
        t.observe = MagicMock(side_effect=[a, b, b, b])
        t.store = MagicMock()
        t.store.lease.return_value = a["lease"]
        t.workers = MagicMock(side_effect=lambda n: {s: {"running": True} for s in base.SERVICES} if n == "physical" else {})
        with patch.object(base.time, "sleep"), patch("builtins.print"):
            self.assertEqual(t.wait_ready("physical", samples=3), b)
        self.assertEqual(t.observe.call_count, 4)

    def test_helper_hash_is_verified_before_any_python_import(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test-failover.py"
            path.write_text("raise RuntimeError('wrong helper')")
            with patch.object(app.importlib.util, "spec_from_file_location") as load, self.assertRaises(app.Abort):
                app.load_base(Path(folder))
            load.assert_not_called()

    def test_cli_suppresses_unknown_exception_messages(self):
        with patch.object(app, "main", side_effect=ValueError("password=private")), patch("builtins.print") as out:
            self.assertEqual(app.cli(), 2)
            self.assertNotIn("private", str(out.call_args))
