import copy
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location("verify_live_rfid", Path(__file__).parents[1] / "deploy/ha/verify_live_rfid.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class LiveRfidAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 6, 13, 50)
        self.lease = {"enabled": True, "valid": True, "owner": "physical", "epoch": 177}
        self.baseline = {"lease": dict(self.lease), "local_time": (self.now-timedelta(seconds=30)).isoformat(),
                         "max_id": 704090, "cursor": 704090}
        self.snapshot = {"lease": dict(self.lease), "max_id": 704091, "cursor": 704091,
                         "fresh_rows": [{"id": 704091}]}
        self.spool = {"counts": {"SENT": 100}, "matched_ids": [704091]}

    def test_fresh_sql_uuid_sent_and_aggregator_cursor_are_all_required(self):
        proof = app.check_evidence(self.baseline, self.snapshot, self.spool, self.now)
        self.assertEqual(proof["physical_sent_reads"], 1)
        self.assertEqual(proof["aggregator_cursor"], 704091)
        for changed, reason in (("fresh", "NO_FRESH"), ("sent", "NOT_CONFIRMED_SENT"),
                                ("pending", "DELIVERY_PENDING"), ("cursor", "NOT_CAUGHT_UP")):
            s, q = copy.deepcopy(self.snapshot), copy.deepcopy(self.spool)
            if changed == "fresh": s["fresh_rows"] = []
            if changed == "sent": q["matched_ids"] = []
            if changed == "pending": q["counts"]["PENDING"] = 1
            if changed == "cursor": s["cursor"] = 704090
            with self.subTest(changed=changed), self.assertRaisesRegex(app.Abort, reason):
                app.check_evidence(self.baseline, s, q, self.now)

    def test_changed_executor_epoch_expired_baseline_and_clock_regression_reject_proof(self):
        s = copy.deepcopy(self.snapshot)
        s["lease"]["epoch"] += 1
        with self.assertRaisesRegex(app.Abort, "epoch changed"):
            app.check_evidence(self.baseline, s, self.spool, self.now)
        for now in (self.now+timedelta(minutes=16), self.now-timedelta(minutes=1)):
            with self.assertRaisesRegex(app.Abort, "expired or local clock"):
                app.check_evidence(self.baseline, self.snapshot, self.spool, now)

    def test_spool_is_read_only_and_requires_matching_sent_uuid_and_source_time(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "spool.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE reads(client_uuid TEXT,source_time TEXT,state TEXT)")
            conn.executemany("INSERT INTO reads VALUES(?,?,?)", [
                ("sent", "2026-10-06T13:50:00.123456", "SENT"),
                ("pending", "2026-10-06T13:50:00.123456", "PENDING"),
                ("wrong-time", "2026-10-06T12:00:00", "SENT")])
            conn.commit(); conn.close()
            before = path.read_bytes()
            rows = [{"id": i, "uuid": uuid, "source_time": "2026-10-06T13:50:00.123"}
                    for i, uuid in enumerate(("sent", "pending", "wrong-time", "absent"), 1)]
            out = app.spool_evidence(path, rows)
            self.assertEqual(out["matched_ids"], [1])
            self.assertEqual(out["counts"]["PENDING"], 1)
            self.assertEqual(before, path.read_bytes())
            missing = Path(folder) / "missing.sqlite"
            with self.assertRaises(app.Abort): app.spool_evidence(missing, rows)
            self.assertFalse(missing.exists())

    def test_all_three_roles_and_five_services_must_be_ready_at_the_same_epoch(self):
        peers = {n: {"node": n, "release_sha": app.RELEASE, "fencing_protocol": 2, "sample_age": 1,
            "operator_maintenance": False, "faulted": False, "prepared": True, "epoch": 177,
            "active": n == "physical", "healthy": n == "physical",
            "services": {s: {"ok": True} for s in app.SERVICES} if n == "physical" else {}}
            for n in app.NODES}
        controller = {"owner": "comparator", "valid": True}
        self.assertTrue(app.healthy_cluster(self.lease, controller, peers))
        bad = copy.deepcopy(peers)
        bad["physical"]["services"]["RfidReader"]["ok"] = False
        self.assertFalse(app.healthy_cluster(self.lease, controller, bad))
        for field, value in (("epoch", 175), ("faulted", True), ("prepared", False),
                             ("operator_maintenance", True), ("sample_age", 15), ("release_sha", "old")):
            bad = copy.deepcopy(peers); bad["perimetr"][field] = value
            self.assertFalse(app.healthy_cluster(self.lease, controller, bad))

    def test_sql_queries_are_selects_and_fresh_source_time_is_required_not_just_delivery_time(self):
        conn = MagicMock()
        def execute(sql, *params):
            result = MagicMock()
            if sql.startswith("SELECT Enabled"): result.fetchone.return_value = (True, "physical", 177, 1)
            elif sql.startswith("SELECT Owner"): result.fetchone.return_value = ("comparator", 1)
            elif sql.startswith("SELECT ISNULL"): result.fetchone.return_value = (704091,)
            elif sql.startswith("SELECT StateValue"): result.fetchone.return_value = ("704091",)
            else: result.fetchall.return_value = [(704091, "uuid", self.now, self.now)]
            return result
        conn.execute.side_effect = execute
        result = app.sql_snapshot(conn, self.baseline)
        self.assertEqual(result["fresh_rows"][0]["id"], 704091)
        calls = conn.execute.call_args_list
        self.assertTrue(all(c.args[0].startswith("SELECT ") for c in calls))
        query = calls[-1].args[0]
        self.assertIn("TOP(200)", query)
        self.assertIn("SourceReaderTime>=?", query)
        self.assertIn("ReceivedAt>=?", query)
        self.assertEqual(calls[-1].args[1], self.baseline["max_id"])
        self.assertEqual(calls[-1].args[2], datetime.fromisoformat(self.baseline["local_time"]))
        conn.commit.assert_not_called()

    def test_unknown_exception_does_not_export_connection_string(self):
        with patch.object(app, "main", side_effect=RuntimeError("PWD=private")), patch("builtins.print") as out:
            self.assertEqual(app.cli(), 2)
            self.assertNotIn("private", str(out.call_args))


if __name__ == "__main__":
    unittest.main()
