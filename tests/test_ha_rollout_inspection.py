"""Inspection must preserve data and prevent credentials from leaving the host."""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location("inspect_rollout", Path(__file__).parents[1] / "deploy/ha/inspect_rollout.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class InspectionTests(unittest.TestCase):
    def test_brief_retains_peer_dependency_cause_metrics_and_lease_without_mutating_evidence(self):
        report = {"node": "physical", "sql": {"lease": [{"Owner": "perimetr", "Epoch": 156}],
                    "triggers": ["large-table"]}, "http": {
            "agent:perimetr": {"http": 200, "body": {"node": "perimetr", "faulted": False,
                "operator_maintenance": False, "services": {"RfidReader": {"ok": False, "detail": {
                    "status": "degraded", "metrics": {"business_flow_latched": True},
                    "dependencies": {"business_flow": {"status": "unavailable",
                        "detail": "rfid_business_flow_fault_latched", "data_age_seconds": 900,
                        "updated_at": "omit-repeated-field"}}}}}}},
            "service:RfidReader": {"error": "URLError"}},
            "logs": {"RfidReader": "x" * 3000 + "partial-secret\nKnownError\n", "Aggregator": "trace"}}
        before = json.dumps(report)
        out = app.brief_report(report)
        service = out["peers"]["perimetr"]["services"]["RfidReader"]
        self.assertEqual(service["detail"]["dependencies"]["business_flow"]["detail"], "rfid_business_flow_fault_latched")
        self.assertTrue(service["detail"]["metrics"]["business_flow_latched"])
        self.assertEqual(out["sql"]["lease"], report["sql"]["lease"])
        self.assertEqual(out["local_services"]["RfidReader"]["error"], "URLError")
        self.assertNotIn("partial-secret", json.dumps(out))
        self.assertNotIn("updated_at", json.dumps(out))
        self.assertEqual(before, json.dumps(report))

    def test_audit_exports_only_event_identity_and_typed_safe_fields(self):
        with tempfile.TemporaryDirectory() as work:
            state = Path(work)
            previous = {"time": 1791273600, "node": "comparator", "kind": "failover",
                        "previous": "physical", "target": "perimetr", "token": "private-controller"}
            current = {"time": 1791273700, "node": "physical", "kind": "node_error", "error": "OperationalError",
                       "epoch": 142, "argv": ["private-argument"], "password": "private-password", "sql": "PWD=private"}
            (state / "audit.previous.jsonl").write_text(json.dumps(previous) + "\n")
            path = state / "audit.jsonl"
            path.write_text(json.dumps(current) + "\npartial-json-token")
            before = path.read_bytes()
            result = app.audit_snapshot(state)
            text = json.dumps(result)
            self.assertNotIn("private", text)
            self.assertNotIn("PWD", text)
            self.assertEqual([e["kind"] for e in result["events"]], ["failover", "node_error"])
            self.assertEqual(result["events"][1]["error"], "OperationalError")
            self.assertEqual(result["malformed_lines"], 1)
            self.assertEqual(before, path.read_bytes())

    def test_audit_is_bounded_and_ignores_malformed_or_unknown_payloads(self):
        with tempfile.TemporaryDirectory() as work:
            state = Path(work)
            event = {"time": float("nan"), "node": "physical", "kind": "promoted", "epoch": 134,
                     "service": [], "action": {}, "target": []}
            path = state / "audit.jsonl"
            path.write_text("x" * (300 * 1024) + "partial-password\n" + json.dumps(event) + "\n"
                            + json.dumps({"node": "physical", "kind": "arbitrary-secret", "password": "unknown"}) + "\n"
                            + json.dumps({"node": "physical", "kind": []}) + "\n")
            result = app.audit_snapshot(state)
            self.assertEqual(result["tail_bytes_per_file"], 256 * 1024)
            self.assertEqual(result["events"], [{"node": "physical", "kind": "promoted", "epoch": 134}])
            self.assertFalse(result["files"]["audit.previous.jsonl"]["exists"])
            self.assertNotIn("password", json.dumps(result))

    def test_redaction_handles_quoted_braced_and_multiline_secrets_without_corrupting_json(self):
        password = 'p; a}ss"\\word\nnext'
        env = {"DST_PASSWORD": password, "PERIMETER_HA_TOKEN": "private-bearer-token"}
        value = {"trace": "login: " + password + " escaped: " + password.replace("}", "}}"),
                 "status": {"token": "unknown-token", "healthy": False},
                 "url": "rtsp://unknown-user:unknown-password@camera/stream"}
        result = app.scrub(value, env)
        text = json.dumps(result)
        self.assertNotIn("unknown-password", text)
        self.assertNotIn("unknown-token", text)
        self.assertNotIn("word", text)
        self.assertFalse(result["status"]["healthy"])
        self.assertEqual(result["status"]["token"], "REDACTED")

    def test_exception_text_never_discloses_credentials(self):
        result = app.error(RuntimeError("PWD=secret; error 15664 and 51001"))
        self.assertEqual(result, {"error": "RuntimeError", "sql_codes": ["15664", "51001"]})

    def test_spool_inspection_is_read_only_and_preserves_pending_rows(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "spool.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE reads(state TEXT)")
            conn.executemany("INSERT INTO reads VALUES(?)", [("PENDING",), ("PENDING",), ("SENT",)])
            conn.commit(); conn.close()
            before = path.read_bytes()
            result = app.spool_snapshot(path, "rfid")
            self.assertEqual(result["counts"], {"PENDING": 2, "SENT": 1})
            self.assertEqual(path.read_bytes(), before)
            missing = Path(work) / "missing.sqlite"
            self.assertFalse(app.spool_snapshot(missing, "rfid")["exists"])
            self.assertFalse(missing.exists())

    def test_log_tail_omits_truncated_secrets_and_unknown_credential_lines(self):
        with tempfile.TemporaryDirectory() as work:
            state = Path(work)
            (state / "logs").mkdir()
            path = state / "logs/RfidReader.log"
            path.write_text("x" * 5000 + "part-of-secret\nTraceback: failure\nPWD=unknown\n"
                            "rtsp://other:pass@camera\nError 51001\nknown-password\n", encoding="utf-8")
            text = app.local_logs(state, {"DST_PASSWORD": "known-password"})["RfidReader"]
            self.assertIn("Error 51001", text)
            self.assertNotIn("part-of-secret", text)
            self.assertNotIn("unknown", text)
            self.assertNotIn("known-password", text)
            self.assertNotIn("other:pass", text)

    def test_sql_inspection_executes_only_selects_and_closes_connection(self):
        driver = MagicMock()
        conn = driver.connect.return_value
        conn.execute.return_value.description = [("column",)]
        conn.execute.return_value.fetchall.return_value = [(1,)]
        conn.execute.return_value.fetchone.return_value = (1,)
        with patch.dict("sys.modules", {"pyodbc": driver}):
            app.sql_snapshot({"PERIMETER_HA_SQL": "private-connection"})
        self.assertTrue(all(c.args[0].lstrip().upper().startswith("SELECT ") for c in conn.execute.call_args_list))
        conn.commit.assert_not_called()
        conn.close.assert_called_once()
        self.assertFalse(driver.pooling)


if __name__ == "__main__":
    unittest.main()
