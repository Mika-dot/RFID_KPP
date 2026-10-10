"""Factory regression: a quiet checkpoint must not replay an old passage as a failure."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common.business_flow import assess_rfid_flow, source_marker
from common.observability import HealthReporter
from deploy.monitored_rfid import _PersistentFlowState, _BusinessFlowWatchdog
from deploy import monitored_rfid_recovery as recovery


class RfidEvidenceOrderingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 6, 18, 20, 58)
        self.rfid = self.now - timedelta(seconds=1087.5)
        self.video = self.now - timedelta(seconds=1089.6)
        self.warehouse = self.now - timedelta(seconds=1257.7)

    def assess(self, **changes):
        values = dict(now=self.now, rfid_at=self.rfid, video_at=self.video,
                      warehouse_at=self.warehouse, video_recent_events=2,
                      warehouse_recent_events=33, stall_seconds=900)
        values.update(changes)
        return assess_rfid_flow(**values)

    def latched(self, **changes):
        values = dict(fault_latched=True, latched_rfid_marker=source_marker(self.rfid),
                      last_fault_detail="rfid_stale_while_video_and_warehouse_active",
                      video_history_complete=True, video_recent_events=0, warehouse_recent_events=0)
        values.update(changes)
        return self.assess(**values)

    def test_observed_factory_events_before_last_read_do_not_prove_a_silent_fault(self):
        result = self.assess()
        self.assertEqual(result.status, "ok")
        self.assertFalse(result.latch_fault)

    def test_equal_video_rfid_timestamp_is_not_evidence_after_the_read(self):
        self.assertFalse(self.assess(video_at=self.rfid).latch_fault)

    def test_later_warehouse_without_later_physical_passage_is_only_advisory(self):
        result = self.assess(warehouse_at=self.now - timedelta(seconds=20))
        self.assertEqual(result.status, "degraded")
        self.assertFalse(result.latch_fault)

    def test_real_activity_after_last_read_still_latches_a_failure(self):
        for count, warehouse_count in ((2, 0), (1, 1)):
            result = self.assess(video_at=self.now - timedelta(seconds=10),
                                 warehouse_at=self.now - timedelta(seconds=20),
                                 video_recent_events=count, warehouse_recent_events=warehouse_count)
            self.assertEqual(result.status, "unavailable")
            self.assertTrue(result.latch_fault)

    def test_complete_history_contradiction_retains_latch_as_visible_warning(self):
        result = self.latched()
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.detail, "rfid_historical_activity_evidence_contradicted")
        self.assertFalse(result.clear_latch)
        self.assertFalse(result.latch_fault)

    def test_evidence_expiring_or_missing_does_not_invalidate_a_confirmed_fault(self):
        for values in ({"video_at": None}, {"video_history_complete": False},
                       {"video_at": self.rfid + timedelta(seconds=10), "video_recent_events": 0},
                       {"last_fault_detail": "state_file_invalid"}, {"last_fault_detail": ""},
                       {"video_recent_events": 1},
                       {"latched_rfid_marker": "different-marker"}):
            with self.subTest(values=values):
                self.assertEqual(self.latched(**values).status, "unavailable")

    def test_new_fresh_read_is_still_the_only_way_to_clear_the_latch(self):
        result = self.latched(rfid_at=self.now - timedelta(seconds=1))
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.clear_latch)

    def test_sql_counts_are_bounded_by_last_read_not_just_the_window(self):
        queries = []
        answers = iter([(self.now,), (self.rfid,), (self.video,), (0,), (self.warehouse,), (0,)])
        cursor = SimpleNamespace(execute=lambda query, *args: queries.append((query, args)),
                                 fetchone=lambda: next(answers), close=lambda: None)
        conn = SimpleNamespace(cursor=lambda: cursor, close=lambda: None)
        watchdog = _BusinessFlowWatchdog.__new__(_BusinessFlowWatchdog)
        watchdog.connection_string = "not-a-real-credential"
        watchdog.evidence_window_sec = 1800
        with patch.dict("sys.modules", {"pyodbc": SimpleNamespace(connect=lambda *a, **k: conn)}):
            snapshot = watchdog._query_snapshot()
        self.assertEqual(snapshot[-2:], (0, 0))
        self.assertIn("MAX(COALESCE", queries[2][0])
        self.assertIn("MAX(Dt)", queries[4][0])
        counts = [q for q in queries if "COUNT_BIG" in q[0]]
        self.assertEqual(len(counts), 2)
        for query, params in counts:
            self.assertIn("> CAST(? AS datetime2)", query)
            self.assertEqual(params, (-1800, self.rfid))

    def test_out_of_order_warehouse_inserts_do_not_hide_one_video_missing_read(self):
        warehouse_at = self.now - timedelta(seconds=20)
        answers = iter([(self.now,), (self.rfid,), (self.now - timedelta(seconds=10),),
                        (1,), (warehouse_at,), (1,)])
        cursor = SimpleNamespace(execute=lambda *_args: None, fetchone=lambda: next(answers), close=lambda: None)
        conn = SimpleNamespace(cursor=lambda: cursor, close=lambda: None)
        watchdog = _BusinessFlowWatchdog.__new__(_BusinessFlowWatchdog)
        watchdog.connection_string, watchdog.evidence_window_sec = "offline", 1800
        with patch.dict("sys.modules", {"pyodbc": SimpleNamespace(connect=lambda *a, **k: conn)}):
            now, rfid, video, warehouse, video_count, warehouse_count = watchdog._query_snapshot()
        result = assess_rfid_flow(now=now, rfid_at=rfid, video_at=video, warehouse_at=warehouse,
                                  video_recent_events=video_count, warehouse_recent_events=warehouse_count,
                                  stall_seconds=900)
        self.assertTrue(result.latch_fault)

    def test_production_loop_does_not_reconnect_or_mutate_contradicted_latch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = _PersistentFlowState(path)
            state.latch(source_marker(self.rfid), "rfid_stale_while_video_and_warehouse_active")
            state.register_restart()
            original = path.read_bytes()
            watchdog = recovery._BusinessFlowWatchdog.__new__(recovery._BusinessFlowWatchdog)
            watchdog.state = state
            watchdog.stall_sec = 900
            watchdog.min_video_events = 2
            watchdog.interval_sec = 15
            watchdog.max_restarts = 3
            watchdog.last_reported_state = ""
            watchdog.unavailable_since = None
            watchdog.reporter = Mock()
            watchdog.meta = SimpleNamespace(last_mode="session", last_attempt=1,
                                             seconds_since_attempt=lambda: 10)
            watchdog._query_snapshot = lambda: (self.now, self.rfid, self.video, self.warehouse, 0, 0)
            watchdog._request_recovery = Mock()
            with patch.object(recovery, "flush_sentry"), patch.object(recovery.time, "sleep", side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    watchdog._loop()
            watchdog._request_recovery.assert_not_called()
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(state.fault_latched)
            calls = watchdog.reporter.set_dependency.call_args_list
            self.assertEqual(calls[-1].args[:2], ("business_flow", "degraded"))

    def test_warning_readiness_still_requires_all_real_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            names = ("business_flow", "rfid_reader", "rfid_tcp", "database", "delivery_writer", "reader_loop", "local_spool")
            reporter = HealthReporter("Perimeter.RfidReader", "test", names, Path(folder) / "heartbeat.json")
            for name in names:
                reporter.set_dependency(name, "ok")
            reporter.set_metric("business_flow_latched", True)
            reporter.set_dependency("business_flow", "degraded", detail=self.latched().detail)
            payload, code = reporter.snapshot()
            self.assertEqual(code, 200)
            self.assertTrue(payload["metrics"]["business_flow_latched"])
            self.assertIn("business_flow", payload["warnings"])
            for name in names[1:]:
                reporter.set_dependency(name, "unavailable")
                self.assertEqual(reporter.snapshot()[1], 503)
                reporter.set_dependency(name, "ok")
            reporter.dependencies["business_flow"]["_updated_mono"] -= 1000
            self.assertEqual(reporter.snapshot()[1], 503)


if __name__ == "__main__":
    unittest.main()
