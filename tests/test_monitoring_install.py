import copy
import importlib.util
import io
import json
import sys
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import MagicMock, Mock, patch

SPEC = importlib.util.spec_from_file_location("ha_monitor_install", Path(__file__).parents[1] / "deploy/ha/install_monitoring.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class MonitorTests(unittest.TestCase):
    def probe(self, node="physical", code=200, **flags):
        payload = dict(node=node, active=False, healthy=False, prepared=True, faulted=False, epoch=189, **{})
        payload.update(flags)
        with patch.object(m, "http_json", return_value=(code, payload)), patch.object(m, "observer_token", return_value=None):
            return m.probe((node, m.NODES[node]))

    def cluster(self):
        return [self.probe(active=True, healthy=True), self.probe("perimetr"), self.probe("comparator")]

    def test_prepared_reserve_is_healthy_without_workers(self):
        result = self.probe("perimetr")
        self.assertEqual(result["severity"], 0)
        self.assertEqual(result["role"], "РЕЗЕРВ")
        self.assertEqual(result["healthy"], 0)

    def test_503_body_preserves_actual_fault_and_epoch(self):
        result = self.probe(code=503, prepared=False, faulted=True)
        self.assertEqual((result["reachable"], result["faulted"], result["epoch"], result["http"]), (1, 1, 189, 503))
        self.assertEqual(result["severity"], 1)

    def test_active_not_ready_is_critical(self):
        result = self.probe(code=503, active=True, healthy=False)
        self.assertEqual(result["severity"], 2)

    def test_no_leader_is_critical(self):
        rows = self.cluster()
        rows[0].update(active=0, severity=0)
        self.assertEqual(m.summarize(rows, (0, "Без предупреждений"))["severity"], 2)

    def test_two_leaders_are_critical(self):
        rows = self.cluster()
        rows[1].update(active=1, healthy=1)
        data = m.summarize(rows, (0, "Без предупреждений"))
        self.assertEqual(data["severity"], 2)
        self.assertEqual(data["active_count"], 2)

    def test_one_good_leader_two_prepared_reserves(self):
        data = m.summarize(self.cluster(), (0, "Без предупреждений"))
        self.assertEqual((data["severity"], data["ready_reserves"]), (0, 2))

    def test_missing_reserve_is_warning(self):
        rows = self.cluster()
        rows[2].update(reachable=0)
        self.assertEqual(m.summarize(rows, (0, "Без предупреждений"))["severity"], 1)

    def test_business_advisory_is_separate_from_operational_readiness(self):
        raw = {"dependencies": {"business_flow": {"status": "degraded"}}, "warnings": {"business_flow": {"detail": "rfid_stale_with_partial_activity_evidence"}}}
        business = m.business_status(raw)
        data = m.summarize(self.cluster(), business)
        self.assertEqual(data["severity"], 1)
        self.assertEqual(data["active_count"], 1)
        self.assertEqual(data["ready_reserves"], 2)

    def test_missing_business_data_is_not_green(self):
        self.assertEqual(m.business_status({})[0], 1)

    def test_stale_cache_cannot_remain_green(self):
        data = m.summarize(self.cluster(), (0, "Без предупреждений"))
        stale = m.freshness(data, now=data["timestamp"]+26)
        self.assertEqual(stale["severity"], 2)
        self.assertEqual(data["severity"], 0)
        self.assertTrue(all(n["role"] == "НЕИЗВЕСТНО" for n in stale["nodes"].values()))

    def test_string_false_is_rejected(self):
        self.assertNotEqual(self.probe(active="false")["severity"], 0)

    def test_other_nodes_identity_is_rejected(self):
        with patch.object(m, "http_json", return_value=(200, dict(node="intruder"))), patch.object(m, "observer_token", return_value=None):
            result = m.probe(("physical", m.NODES["physical"]))
        self.assertEqual(result["reachable"], 0)
        self.assertEqual(result["detail"], "NodeIdentityMismatch")

    def test_network_error_cannot_leak_secret(self):
        with patch.object(m, "http_json", side_effect=Exception("secret-url-password")):
            result = m.probe(("physical", m.NODES["physical"]))
        self.assertNotIn("secret", json.dumps(result))

    def test_authenticated_snapshot_detects_epoch_transition(self):
        raw = dict(node="physical", active=True, healthy=True, prepared=True, faulted=False, epoch=189)
        full = dict(raw, epoch=190, sample_age=1, fencing_protocol=2)
        with patch.object(m, "observer_token", return_value="a"*64), patch.object(m, "http_json", side_effect=[(200, raw), (200, full)]):
            result = m.probe(("physical", m.NODES["physical"]))
        self.assertEqual(result["severity"], 2)
        self.assertEqual(result["detail"], "AgentChangedDuringProbe")

    def test_authenticated_data_uses_known_service_ok_field(self):
        raw = dict(node="physical", active=True, healthy=False, prepared=False, faulted=True, epoch=189)
        services = {n: {"ok": n != "Yolo"} for n in ("RfidReader", "RusGuardSync", "Yolo", "Aggregator", "WebDashboard")}
        full = dict(raw, sample_age=1, services=services, release_sha="a"*40, fencing_protocol=2)
        with patch.object(m, "observer_token", return_value="b"*64), patch.object(m, "http_json", side_effect=[(503, raw), (200, full)]):
            result = m.probe(("physical", m.NODES["physical"]))
        self.assertEqual(result["detail"], "Не готовы: Yolo")
        self.assertEqual(result["release"], "a"*40)

    def test_actual_http_503_json_is_read(self):
        raw = json.dumps(dict(node="physical", faulted=True)).encode()
        error = HTTPError("http://172.31.0.188:18200/health/ready", 503, "Degraded", {}, io.BytesIO(raw))
        class Opener:
            def open(self, *args, **kwargs):
                raise error
        with patch.object(m, "build_opener", return_value=Opener()):
            code, value = m.http_json(error.url, accept_degraded=True)
        self.assertEqual(code, 503)
        self.assertIs(value["faulted"], True)

    def test_credentials_only_go_to_their_known_destination(self):
        with self.assertRaisesRegex(ValueError, "CredentialDestinationRefused"):
            m.http_json("http://external.invalid", token="grafana-secret")
        with self.assertRaisesRegex(ValueError, "CredentialDestinationRefused"):
            m.http_json("http://172.31.0.188:18200/maintenance", ha_token="ha-secret")
        with self.assertRaisesRegex(ValueError, "CredentialDestinationRefused"):
            m.http_json("http://127.0.0.1:3000", ha_token="ha-secret")
        self.assertIsNone(m.NoRedirects().redirect_request(None, None, 302, "", {}, "http://external.invalid"))

    def test_observer_token_can_reach_local_behavior_service(self):
        response=MagicMock();response.__enter__.return_value=response
        response.code=200;response.read.return_value=b"{}"
        opener=Mock();opener.open.return_value=response
        with patch.object(m, "build_opener", return_value=opener):
            code, payload=m.http_json(m.OBSERVER_STATUS, ha_token="a"*64)
        self.assertEqual((200, {}), (code, payload))

    def test_dashboard_update_preserves_existing_panels_and_is_idempotent(self):
        original = {"uid": "existing", "version": 16, "panels": [{"id": 900, "type": "volkovlabs-echarts-panel", "options": {"script": "original"}, "gridPos": {"x": 0, "y": 0, "w": 24, "h": 28}}]}
        baseline = copy.deepcopy(original)
        first = m.make_panels(original)
        second = m.make_panels(first)
        self.assertEqual(first, second)
        self.assertEqual(original, baseline)
        preserved = first["panels"][4]
        self.assertEqual(preserved["options"], baseline["panels"][0]["options"])
        self.assertEqual(preserved["gridPos"]["y"], 12)
        self.assertTrue(all(t["url"].startswith("http://127.0.0.1:19150/") for p in first["panels"][:4] for t in p["targets"]))
        self.assertTrue(all(p["options"]["reduceOptions"]["fields"] == "/.*/" for p in first["panels"][:3]))

    def test_incompatible_or_missing_fencing_protocol_cannot_be_green(self):
        for protocol in (None, 1, "2", 2.0):
            raw = dict(node="physical", active=True, healthy=True, prepared=True, faulted=False, epoch=189)
            full = dict(raw, sample_age=1, fencing_protocol=protocol)
            with patch.object(m, "observer_token", return_value="a"*64), \
                 patch.object(m, "http_json", side_effect=[(200, raw), (200, full)]):
                result = m.probe(("physical", m.NODES["physical"]))
            self.assertEqual(2, result["severity"])
            self.assertEqual("FencingProtocolMismatch", result["detail"])

    def test_existing_panel_id_cannot_be_overwritten(self):
        with self.assertRaisesRegex(ValueError, "DashboardPanelIdCollision"):
            m.make_panels({"panels": [{"id": 191520, "description": "another application"}]})

    def test_wallboard_adapter_preserves_original_routes(self):
        import http.server
        class OriginalHandler:
            def do_GET(self):
                self.original_called = True
            def send_response(self, code):
                self.code = code
            def send_header(self, *args):
                pass
            def end_headers(self):
                pass
        captured = {}
        class FakeServer:
            def __init__(self, address, handler, *args, **kwargs):
                captured["handler"] = handler
        def run(*args, **kwargs):
            http.server.HTTPServer(("127.0.0.1", 19150), OriginalHandler)
        with patch.object(http.server, "HTTPServer", FakeServer), patch.object(http.server, "ThreadingHTTPServer", FakeServer), patch("runpy.run_path", side_effect=run), patch.object(sys, "argv", []), patch.object(sys, "path", list(sys.path)):
            m.proxy_wallboard("/opt/mositlab-wallboard/wallboard.py")
            handler = captured["handler"]()
            handler.path = "/projects"
            handler.do_GET()
            self.assertTrue(handler.original_called)
            handler.path = "/perimeter-ha/summary"
            handler.wfile = io.BytesIO()
            with patch.object(m, "http_json", side_effect=ConnectionError("private-token")):
                handler.do_GET()
            data = json.loads(handler.wfile.getvalue())
            self.assertEqual(data[0]["severity"], 2)
            self.assertNotIn("private-token", handler.wfile.getvalue().decode())
