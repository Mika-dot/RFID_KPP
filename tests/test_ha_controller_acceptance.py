"""Check controller failure/recovery guards without stopping native services."""
import copy
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location("controller_acceptance", Path(__file__).parents[1] /
                                            "deploy/ha/test_controller_fallback.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def snapshot(owner="physical", controller="perimetr", absent=False, epoch=142):
    names = app.NODES[:2] if absent else app.NODES
    return {"lease": {"enabled": True, "valid": True, "owner": owner, "epoch": epoch},
            "controller": {"owner": controller, "valid": True}, "consistent": True,
            "nodes": {n: {"active": n == owner, "healthy": n == owner, "prepared": True,
                          "faulted": False, "operator_maintenance": False, "epoch": epoch,
                          "services": {s: {"ok": True} for s in app.SERVICES} if n == owner else {}}
                      for n in names}}


def native():
    return {"User": "perimeter", "Restart": "always", "KillMode": "control-group", "UnitFileState": "enabled",
            "WorkingDirectory": "/opt/perimeter/source", "ActiveState": "active", "SubState": "running", "MainPID": "1",
            "ExecStart": "{ path=/opt/perimeter/venv/bin/python ; argv[]=/opt/perimeter/venv/bin/python "
                         "/opt/perimeter/source/guardian/boot.py --config /etc/perimeter/node.json ; ignore_errors=no ; }"}


class ControllerAcceptanceTests(unittest.TestCase):
    def test_fallback_requires_real_controller_validity_five_services_and_current_epoch(self):
        original = snapshot(absent=True)
        self.assertTrue(app.ready(original, "physical", {"perimetr"}, True))
        for section, key, value in (("controller", "owner", "comparator"), ("controller", "valid", False),
                                    ("lease", "valid", False), ("lease", "enabled", False), ("lease", "epoch", 143)):
            s = copy.deepcopy(original)
            s[section][key] = value
            self.assertFalse(app.ready(s, "physical", {"perimetr"}, True))
        s = copy.deepcopy(original)
        s["nodes"]["physical"]["services"]["RfidReader"]["ok"] = False
        self.assertFalse(app.ready(s, "physical", {"perimetr"}, True))
        self.assertFalse(app.ready({**original, "consistent": False}, "physical", {"perimetr"}, True))

    def test_only_stopped_node_can_be_absent_and_final_recovery_requires_both_reserves(self):
        self.assertFalse(app.ready(snapshot(absent=True), "physical", {"perimetr"}))
        for owner in ("physical", "perimetr"):
            s = snapshot(owner, absent=True)
            other = "perimetr" if owner == "physical" else "physical"
            s["nodes"][other].update(faulted=True, prepared=False)
            self.assertTrue(app.ready(s, owner, {"perimetr"}, True))
        s = snapshot()
        s["nodes"]["comparator"]["faulted"] = True
        self.assertFalse(app.ready(s, "physical", {"perimetr", "comparator"}))

    def test_native_service_identity_checks_argv_user_root_and_group_shutdown(self):
        p = native()
        self.assertTrue(app.native_identity(p))
        for key, value in (("User", "root"), ("KillMode", "process"), ("UnitFileState", "disabled"),
                           ("WorkingDirectory", "/opt/other"), ("ExecStart", p["ExecStart"].replace("--config", "--other"))):
            self.assertFalse(app.native_identity({**p, key: value}))

    def test_unit_identity_change_refuses_native_stop(self):
        t = app.Test.__new__(app.Test)
        t.native = MagicMock(return_value={**native(), "User": "root"})
        with patch.object(app.subprocess, "run") as run, self.assertRaises(app.Abort):
            t.service("stop")
        run.assert_not_called()

    def test_comparator_executor_role_change_refuses_stop(self):
        t = app.Test.__new__(app.Test)
        t.observe = MagicMock(return_value=snapshot("comparator", "comparator"))
        t.service, t.workers = MagicMock(), MagicMock()
        with self.assertRaises(app.Abort):
            t.stop_passive()
        t.service.assert_not_called()

    def test_comparator_workers_prevent_stop_even_when_status_claims_passive(self):
        t = app.Test.__new__(app.Test)
        t.observe = MagicMock(return_value=snapshot(controller="comparator"))
        t.workers = MagicMock(return_value={"RfidReader": {"running": True}})
        t.service = MagicMock()
        with self.assertRaises(app.Abort):
            t.stop_passive()
        t.service.assert_not_called()

    def test_only_native_local_service_is_stopped_after_passive_proof(self):
        t = app.Test.__new__(app.Test)
        t.observe = MagicMock(return_value=snapshot(controller="comparator"))
        t.workers = MagicMock(return_value={})
        t.store = MagicMock()
        t.store.lease.return_value = snapshot(controller="comparator")["lease"]
        t.service, t.phase = MagicMock(), MagicMock()
        t.stopped = MagicMock(return_value=True)
        t.stop_passive()
        t.service.assert_called_once_with("stop")

    def test_changed_lease_after_worker_probe_prevents_stop(self):
        t = app.Test.__new__(app.Test)
        t.observe = MagicMock(return_value=snapshot(controller="comparator"))
        t.workers = MagicMock(return_value={})
        t.store = MagicMock()
        t.store.lease.return_value = snapshot("comparator", "comparator", epoch=144)["lease"]
        t.service = MagicMock()
        with self.assertRaises(app.Abort):
            t.stop_passive()
        t.service.assert_not_called()

    def test_any_answering_agent_prevents_an_offline_claim(self):
        t = app.Test.__new__(app.Test)
        t.nodes, t.token = {"comparator": {"url": "http://comparator"}}, "test"
        t.get_json = MagicMock(return_value=(200, {}))
        self.assertFalse(t.offline())
        t.get_json.return_value = (401, {})
        self.assertFalse(t.offline())
        t.get_json.side_effect = app.URLError("connection refused")
        self.assertTrue(t.offline())

    def test_takeover_failure_restarts_comparator_and_never_reports_success(self):
        t = app.Test.__new__(app.Test)
        t.cfg = {"state_dir": "unused-controller-test-path"}
        t.precheck, t.private_directory, t.phase, t.stop_passive = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        t.wait_ready = MagicMock(side_effect=app.Abort("No Perimetr controller"))
        t.recover = MagicMock()
        with patch("builtins.print"), self.assertRaises(app.Abort):
            t.run()
        t.recover.assert_called_once()
        self.assertFalse(t.record["fallback_proven"])
        self.assertNotIn(("CONTROLLER_FALLBACK_TEST_OK",), [c.args for c in t.phase.call_args_list])

    def test_precheck_failure_does_not_stop_or_start_any_service(self):
        t = app.Test.__new__(app.Test)
        t.precheck = MagicMock(side_effect=app.Abort("baseline failed"))
        t.stop_passive, t.recover = MagicMock(), MagicMock()
        with self.assertRaises(app.Abort):
            t.run()
        t.stop_passive.assert_not_called()
        t.recover.assert_not_called()

    def test_controller_change_stale_sample_and_epoch_change_reset_proof_series(self):
        t = app.Test.__new__(app.Test)
        first = snapshot(absent=True)
        second = snapshot(absent=True, epoch=144)
        wrong = snapshot(absent=True, epoch=144, controller="comparator")
        t.observe = MagicMock(side_effect=[app.Abort("stale"), first, second, wrong, second, second, second])
        t.stopped = MagicMock(return_value=True)
        t.offline = MagicMock(return_value=True)
        t.workers = MagicMock(side_effect=lambda n: {s: {"running": True} for s in app.SERVICES} if n == "physical" else {})
        t.store = MagicMock()
        t.store.lease.side_effect = [first["lease"], second["lease"], second["lease"], second["lease"], second["lease"]]
        with patch.object(app.time, "sleep"), patch("builtins.print"):
            result = t.wait_ready({"perimetr"}, absent=True, samples=3)
        self.assertEqual(t.observe.call_count, 7)
        self.assertEqual(result["lease"]["epoch"], 144)

    def test_external_ha_off_is_not_hidden_as_transient_and_no_sql_write_is_attempted(self):
        t = app.Test.__new__(app.Test)
        t.store = MagicMock()
        t.store.lease.return_value = {"enabled": False}
        with self.assertRaises(app.Halt):
            t.observe()
        t.store.connect.assert_not_called()
        t.store.grant.assert_not_called()
        t.observe = MagicMock(side_effect=app.Halt("HA OFF"))
        with self.assertRaises(app.Halt):
            t.wait_ready({"perimetr"})

    def test_recovery_starts_service_and_accepts_valid_elected_controller_without_override(self):
        t = app.Test.__new__(app.Test)
        t.service, t.phase = MagicMock(), MagicMock()
        t.wait_ready = MagicMock(return_value=snapshot())
        with patch("builtins.print"):
            t.recover()
        t.service.assert_called_once_with("start")
        t.wait_ready.assert_called_once_with({"perimetr", "comparator"})

    def test_cli_suppresses_unknown_exception_text_and_interrupt_details(self):
        for exc in (ValueError("password=sensitive"), KeyboardInterrupt("token=sensitive")):
            with patch.object(app, "main", side_effect=exc), patch("builtins.print") as output:
                self.assertEqual(app.cli(), 2)
                self.assertNotIn("sensitive", str(output.call_args))
