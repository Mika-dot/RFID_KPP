"""Offline bounded audit regressions. No production Grafana/API access."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.ha import audit_wallboard_live as audit


class WallboardAuditTests(unittest.TestCase):
    def test_token_not_sent_to_non_grafana_host(self):
        with self.assertRaisesRegex(ValueError, "TokenDestinationRefused"):
            audit.request_json("http://127.0.0.1:19150/projects", "topsecret")
        with self.assertRaisesRegex(ValueError, "UnapprovedLocalDestination"):
            audit.request_json("http://example.org/projects")

    def test_read_token_quotes_and_no_raw_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "wallboard.env"
            env.write_text("# note\nGRAFANA_TOKEN='example-private-token-123 # abc'\n")
            self.assertEqual("example-private-token-123 # abc", audit.token_from_env(env))

    def test_layout_detects_overlaps_but_not_touching(self):
        panels = [
            {"id": 1, "type": "stat", "gridPos": {"x": 0, "y": 0, "w": 12, "h": 4}},
            {"id": 2, "type": "stat", "gridPos": {"x": 12, "y": 0, "w": 12, "h": 4}},
            {"id": 3, "type": "stat", "gridPos": {"x": 11, "y": 1, "w": 4, "h": 2}},
        ]
        self.assertEqual([{"issue": "overlap", "panels": [1, 3]},
                          {"issue": "overlap", "panels": [2, 3]}], audit.panel_positions(panels))

    def test_frames_are_data_aware_and_do_not_copy_values(self):
        payload = {"results": {"A": {"frames": [{"schema": {"fields": [
            {"name": "time", "type": "time"},
            {"name": "tag", "type": "string"}]},
            "data": {"values": [[100, 200], ["SECRET_TAG", None]]}}]}}}
        rows = audit.inspect_frames(payload, ["A"])
        self.assertEqual(rows[0]["rows"], 2)
        self.assertEqual(rows[0]["values"], 1)
        self.assertNotIn("SECRET_TAG", json.dumps(rows))

    def test_audit_success_and_no_raw_event_identity(self):
        requests = []
        def fake(url, token=None, body=None, timeout=10):
            requests.append((url, body is not None))
            if url.endswith("/api/health"):
                return {"database": "ok"}
            if url.endswith("/api/plugins/volkovlabs-echarts-panel/settings"):
                return {"id": audit.PANEL_PLUGIN, "type": "panel"}
            if url.endswith(audit.PLAYLIST_ROUTE):
                return {"spec": {"interval": "1m", "items": [
                    {"value": x} for x in audit.DASHBOARDS]}}
            if url.endswith("/api/datasources/uid/wallboard-api"):
                return {"uid": "wallboard-api", "type": "yesoreyeram-infinity-datasource",
                        "jsonData": {"allowedHosts": [audit.WALLBOARD]}}
            if url.endswith("/api/search?type=dash-db&limit=1000"):
                return [{"title": "MOSITLAB"}]
            if "/api/dashboards/uid/" in url:
                uid = url.rsplit("/", 1)[1]
                return {"dashboard": {"uid": uid, "title": "Safe title", "version": 1,
                    "panels": [{"id": 1, "type": "stat", "title": "Project status",
                                "gridPos": {"x": 0, "y": 0, "w": 4, "h": 4},
                                "datasource": {"uid": "wallboard-api"},
                                "targets": [{"refId": "A", "url": audit.WALLBOARD + "/projects"}]}]}}
            if url.endswith("/api/ds/query"):
                self.assertIsNotNone(body)
                return {"results": {"A": {"frames": [{
                    "schema": {"fields": [{"name": "status", "type": "string"}]},
                    "data": {"values": [["Healthy"]]}}]}}}
            if url.endswith("/projects"):
                return [{"name": "Project A", "status": "ok"}]
            if url.endswith("/perimeter-ha/summary"):
                return {"severity": 0, "leader": "physical",
                        "timestamp": time.time(), "ready_reserves": 2}
            if url.endswith("/perimeter-ha/recent"):
                return [{"event_id": 9, "SourceTag": "SECRET_RFID_EPC_123456"}]
            return {"status": "ok"}
        with patch.object(audit, "request_json", side_effect=fake):
            result = audit.audit("token", timeout=5)
        self.assertEqual(2, result["totals"]["dashboards_loaded"])
        self.assertEqual(2, result["totals"]["panels_ok"])
        self.assertEqual(9, result["totals"]["endpoints_ok"])
        self.assertTrue(result["playlist"]["correct"])
        self.assertNotIn("SECRET_RFID_EPC", json.dumps(result))
        for url, written in requests:
            if written:
                self.assertTrue(url.endswith("/api/ds/query"))

    def test_query_error_has_no_sensitive_error_contents(self):
        def fake(url, token=None, body=None, timeout=10):
            raise RuntimeError("SQL PASSWORD=private-value")
        with patch.object(audit, "request_json", side_effect=fake):
            value = audit.check_panel_query("token", ("dashboard", {
                "id": 1, "title": "Panel", "targets": [{"refId": "A"}]}), 5)
        self.assertEqual("ERROR", value["result"])
        self.assertEqual("RuntimeError", value["error"])
        self.assertNotIn("private-value", str(value))


if __name__ == "__main__":
    unittest.main()
