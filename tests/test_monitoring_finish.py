import copy
import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("ha_monitor_finish", Path(__file__).parents[1] / "deploy/ha/finish_monitoring.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def status(node, **flags):
    result = dict(node=node, fencing_protocol=2, sample_age=1, active=False, healthy=False, prepared=True, faulted=False,
                  operator_maintenance=False, epoch=230, release_sha="a"*40, preflight={"ok": True}, resources={})
    result.update(flags)
    return result


class FinishMonitoringTests(unittest.TestCase):
    def test_skipping_zabbix_still_reports_current_cluster_state(self):
        observer = SimpleNamespace(observer_token=lambda: "secret")
        values = {n: status(n) for n in m.AGENTS}
        values["physical"].update(active=True, healthy=True)
        output = io.StringIO()
        with patch.object(m.os, "geteuid", return_value=0, create=True), patch.object(m, "load_observer", return_value=observer), patch.object(m, "snapshot", return_value=values), patch("sys.argv", ["tool", "--configure-zabbix"]), patch("builtins.input", return_value="-"), redirect_stdout(output):
            self.assertEqual(m.main(), 1)
        self.assertIn("ZABBIX_SETUP_PENDING_DIRECT_LOGIN", output.getvalue())
        self.assertIn("FINAL_HA_READY", output.getvalue())
        self.assertNotIn("MONITORING_FINISH_ACTIONS_COMPLETED", output.getvalue())

    def test_grafana_failure_does_not_block_core_diagnosis_or_hide_failure(self):
        observer = SimpleNamespace(observer_token=lambda: "secret")
        values = {n: status(n, faulted=True) for n in m.AGENTS}
        output = io.StringIO()
        with patch.object(m.os, "geteuid", return_value=0, create=True), patch.object(m, "load_observer", return_value=observer), patch.object(m, "snapshot", return_value=values), patch.object(m, "finish_grafana", side_effect=RuntimeError("secret")), patch.object(m, "diagnose") as diagnosis, patch("sys.argv", ["tool", "--finish-grafana", "--diagnose"]), redirect_stdout(output):
            self.assertEqual(m.main(), 1)
            diagnosis.assert_called_once_with("secret")
        self.assertIn("FINAL_HA_NOT_READY", output.getvalue())
        self.assertNotIn("secret", output.getvalue())

    def test_shared_token_and_braced_password_are_scrubbed(self):
        value = {"password": "do-not-print", "logs": "PWD={one;two}; UID=name; token='secret'; rtsp://u:p@host/a abcdef012345"}
        result = m.scrub(value, ("abcdef012345",))
        printed = json.dumps(result)
        for forbidden in ("do-not-print", "one;two", "name", "'secret'", "u:p@", "abcdef012345"):
            self.assertNotIn(forbidden, printed)

    def test_only_passive_prepared_faulted_node_can_be_repaired(self):
        self.assertTrue(m.eligible_passive("perimetr", status("perimetr", faulted=True)))
        for flags in ({"active": True}, {"prepared": False}, {"faulted": False}, {"operator_maintenance": True},
                      {"sample_age": 12}, {"preflight": {"ok": False}}, {"resources": {"restart_required": True}}):
            value = status("perimetr", faulted=True)
            value.update(flags)
            self.assertFalse(m.eligible_passive("perimetr", value), flags)

    def test_agent_request_catalog_cannot_send_promote_or_arbitrary_actions(self):
        for endpoint, payload in (("/promote", {}), ("/maintenance", {"enabled": False}), ("/repair", {"action": "verify", "service": "all"}), ("/repair", {"action": "restart_service", "service": "RfidReader"})):
            with self.assertRaises(ValueError):
                m.ha_request("physical", "fake-secret", endpoint, payload)
        with self.assertRaises(ValueError):
            m.ha_request("external-host", "secret", "/status")

    def test_one_healthy_leader_and_two_passive_ready_reserves(self):
        values = {n: status(n) for n in m.AGENTS}
        values["physical"].update(active=True, healthy=True)
        self.assertTrue(m.cluster_ready(values))
        for flags in ({"healthy": False}, {"faulted": True}, {"operator_maintenance": True}):
            bad = copy.deepcopy(values)
            bad["physical"].update(flags)
            self.assertFalse(m.cluster_ready(bad))
        bad = copy.deepcopy(values)
        bad["perimetr"]["active"] = True
        self.assertFalse(m.cluster_ready(bad))
        bad = copy.deepcopy(values)
        bad["comparator"]["epoch"] = 229
        self.assertFalse(m.cluster_ready(bad))
        self.assertFalse(m.cluster_ready({"physical": values["physical"]}))

    def test_operator_repair_never_posts_to_active_node(self):
        calls = []
        def request(node, token, endpoint, payload=None):
            calls.append((node, endpoint, payload))
            return 200, status(node, active=True, prepared=False, faulted=True)
        values = {n: status(n) for n in m.AGENTS}
        values["physical"].update(active=True, healthy=True)
        with patch.object(m, "ha_request", side_effect=request), patch.object(m, "snapshot", return_value=values), patch.object(m.time, "monotonic", side_effect=[0, 1, 2, 3, 4, 31]), patch.object(m.time, "sleep"), redirect_stdout(io.StringIO()):
            self.assertTrue(m.repair_passive("secret"))
        self.assertTrue(all(payload is None for _, _, payload in calls))

    def test_repair_rechecks_empty_workers_and_epoch(self):
        calls = []
        def request(node, token, endpoint, payload=None):
            calls.append((node, endpoint, payload))
            current = status(node, faulted=True)
            return (200, current) if endpoint == "/status" else (200, {"status": current, "workers": {"RfidReader": {"running": True}}})
        values = {n: status(n) for n in m.AGENTS}
        values["physical"].update(active=True, healthy=True)
        with patch.object(m, "ha_request", side_effect=request), patch.object(m, "snapshot", return_value=values), patch.object(m.time, "monotonic", side_effect=[0, 1, 2, 3, 4, 31]), patch.object(m.time, "sleep"), redirect_stdout(io.StringIO()):
            m.repair_passive("secret")
        self.assertTrue(all(payload is None for _, _, payload in calls))

    def test_direct_zabbix_uses_local_api_and_session_never_in_configuration(self):
        captured = []
        def http(url, **kw):
            captured.append((url, kw))
            method = kw["body"]["method"]
            result = "6.0.9" if method == "apiinfo.version" else "b"*32 if method == "user.login" else []
            return 200, {"result": result}
        rpc = m.direct_zabbix_session(SimpleNamespace(http_json=http), "Admin", "hidden-password")
        rpc("host.get", {"hostids": ["10539"]})
        self.assertTrue(all(url == "http://127.0.0.1/api_jsonrpc.php" for url, _ in captured))
        self.assertEqual(captured[2][1]["body"]["auth"], "b"*32)
        self.assertNotIn("hidden-password", json.dumps(captured[2]))
        with self.assertRaises(ValueError):
            rpc("user.update", {})

    def test_zabbix_error_body_does_not_leak_credentials(self):
        def http(url, **kw):
            if kw["body"]["method"] == "apiinfo.version":
                return 200, {"result": "6.0.9"}
            return 200, {"error": {"code": -32602, "data": "hidden-password"}}
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(RuntimeError):
            m.direct_zabbix_session(SimpleNamespace(http_json=http), "Admin", "hidden-password")
        self.assertNotIn("hidden-password", output.getvalue())

    def test_diagnostics_include_cause_and_preserve_business_latch(self):
        value = status("physical", active=True, healthy=False)
        value["services"] = {"RfidReader": {"ok": False, "detail": {"dependencies": {"business_flow": {"status": "unavailable", "detail": "confirmed_reader_fault"}}, "metrics": {"business_flow_latched": True}}}}
        baseline = copy.deepcopy(value)
        result = m.diagnostic_report(value, {"workers": {}, "logs": {"RfidReader": "token=secret"}}, "secret")
        self.assertTrue(result["services"]["RfidReader"]["metrics"]["business_flow_latched"])
        self.assertEqual(value, baseline)
        self.assertNotIn("secret", json.dumps(result))
