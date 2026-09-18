from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path

from common.business_flow import assess_rfid_flow, source_marker


ROOT = Path(__file__).resolve().parents[1]


class BusinessFlowSelfHealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 18, 18, 0, 0)

    def test_empty_checkpoint_is_not_false_alarm(self) -> None:
        result = assess_rfid_flow(
            now=self.now,
            rfid_at=None,
            video_at=None,
            warehouse_at=None,
            video_recent_events=0,
            warehouse_recent_events=0,
            stall_seconds=900,
        )
        self.assertEqual(result.status, "ok")
        self.assertFalse(result.latch_fault)

    def test_warehouse_only_makes_readiness_degraded_but_does_not_restart(self) -> None:
        result = assess_rfid_flow(
            now=self.now,
            rfid_at=self.now - timedelta(hours=2),
            video_at=None,
            warehouse_at=self.now - timedelta(minutes=2),
            video_recent_events=0,
            warehouse_recent_events=3,
            stall_seconds=900,
        )
        self.assertEqual(result.status, "degraded")
        self.assertFalse(result.latch_fault)

    def test_video_and_warehouse_confirm_silent_rfid_failure(self) -> None:
        old_rfid = self.now - timedelta(hours=2)
        result = assess_rfid_flow(
            now=self.now,
            rfid_at=old_rfid,
            video_at=self.now - timedelta(minutes=1),
            warehouse_at=self.now - timedelta(minutes=2),
            video_recent_events=1,
            warehouse_recent_events=1,
            stall_seconds=900,
        )
        self.assertEqual(result.status, "unavailable")
        self.assertTrue(result.latch_fault)

    def test_multiple_video_passages_are_sufficient_evidence(self) -> None:
        result = assess_rfid_flow(
            now=self.now,
            rfid_at=self.now - timedelta(hours=2),
            video_at=self.now - timedelta(minutes=1),
            warehouse_at=None,
            video_recent_events=2,
            warehouse_recent_events=0,
            stall_seconds=900,
            min_video_events=2,
        )
        self.assertEqual(result.status, "unavailable")
        self.assertTrue(result.latch_fault)

    def test_latched_failure_cannot_turn_green_just_because_evidence_ages_out(self) -> None:
        old_rfid = self.now - timedelta(hours=2)
        result = assess_rfid_flow(
            now=self.now,
            rfid_at=old_rfid,
            video_at=None,
            warehouse_at=None,
            video_recent_events=0,
            warehouse_recent_events=0,
            stall_seconds=900,
            fault_latched=True,
            latched_rfid_marker=source_marker(old_rfid),
        )
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.clear_latch)

    def test_latched_failure_is_not_cleared_by_different_but_stale_backlog_row(self) -> None:
        latched_rfid = self.now - timedelta(hours=3)
        delayed_backlog_rfid = self.now - timedelta(hours=2)
        result = assess_rfid_flow(
            now=self.now,
            rfid_at=delayed_backlog_rfid,
            video_at=None,
            warehouse_at=None,
            video_recent_events=0,
            warehouse_recent_events=0,
            stall_seconds=900,
            fault_latched=True,
            latched_rfid_marker=source_marker(latched_rfid),
        )
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.clear_latch)

    def test_latch_clears_only_after_new_fresh_rfid_read(self) -> None:
        old_rfid = self.now - timedelta(hours=2)
        new_rfid = self.now - timedelta(seconds=5)
        result = assess_rfid_flow(
            now=self.now,
            rfid_at=new_rfid,
            video_at=None,
            warehouse_at=None,
            video_recent_events=0,
            warehouse_recent_events=0,
            stall_seconds=900,
            fault_latched=True,
            latched_rfid_marker=source_marker(old_rfid),
        )
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.clear_latch)

    def test_aggregator_failure_path_does_not_refresh_progress_watchdog(self) -> None:
        text = (ROOT / "deploy" / "monitored_aggregator.py").read_text(encoding="utf-8")
        exception_block = text.split("except Exception as exc:", 1)[1]
        self.assertNotIn('reporter.progress("pipeline")', exception_block)
        self.assertIn("os._exit(73)", exception_block)

    def test_rusguard_failure_path_does_not_refresh_progress_watchdog(self) -> None:
        text = (ROOT / "deploy" / "monitored_rusguard.py").read_text(encoding="utf-8")
        exception_block = text.split("except Exception as exc:", 1)[1]
        self.assertNotIn('reporter.progress("sync_loop")', exception_block)
        self.assertIn("register_progress_watchdog(", text)
        self.assertIn("exit_code=72", text)

    def test_rusguard_source_scan_is_bounded_and_skips_unrelated_history(self) -> None:
        text = (ROOT / "DB_RusGard" / "db_sync_v2.py").read_text(encoding="utf-8")
        self.assertIn("[Log]._id <= ?", text)
        self.assertIn("source_high_watermark", text)
        self.assertIn("set_state(dst, high_watermark)", text)
        self.assertIn("len(rows) < BATCH_SIZE", text)

    def test_rfid_adapter_has_controlled_self_heal_exit(self) -> None:
        text = (ROOT / "deploy" / "monitored_rfid.py").read_text(encoding="utf-8")
        self.assertIn('"business_flow"', text)
        self.assertIn("os._exit(72)", text)
        self.assertIn("RfidInventoryStartFailed", text)

    def test_rfid_main_loop_and_delivery_writer_are_watchdog_protected(self) -> None:
        text = (ROOT / "deploy" / "monitored_rfid.py").read_text(encoding="utf-8")
        self.assertIn('"reader_loop"', text)
        self.assertIn('"delivery_writer"', text)
        self.assertIn("RFID_READER_LOOP_WATCHDOG_SEC", text)
        self.assertIn("RFID_DELIVERY_WRITER_WATCHDOG_SEC", text)
        self.assertIn("exit_code=75", text)
        self.assertIn("exit_code=76", text)
        self.assertIn('reporter.progress("delivery_writer")', text)

    def test_yolo_adapter_restarts_on_blocked_capture_thread(self) -> None:
        text = (ROOT / "deploy" / "monitored_yolo.py").read_text(encoding="utf-8")
        self.assertIn("RFID_RTSP_READ_WATCHDOG_SEC", text)
        self.assertIn("capture_progress_mono", text)
        self.assertIn("capture_thread_blocked", text)
        self.assertIn("os._exit(74)", text)

    def test_production_aggregator_does_not_rollback_after_commit_for_status_failure(self) -> None:
        text = (ROOT / "KPP" / "kpp_aggregator_v3_warehouse_v3.py").read_text(encoding="utf-8")
        process_block = text.split("    def process_once(self) -> int:", 1)[1]
        self.assertIn("conn.commit()", process_block)
        self.assertIn("business state remains committed", process_block)
        self.assertIn("counters_snapshot", process_block)
        self.assertLess(
            process_block.index("conn.commit()"),
            process_block.index("self.maybe_log_status("),
        )
        status_try = process_block.split("self.maybe_log_status(", 1)[1]
        self.assertNotIn("restore_active_snapshot", status_try)

    def test_startup_requires_readiness_from_all_five_services(self) -> None:
        waiter = (ROOT / "deploy" / "wait_for_services_v3.py").read_text(encoding="utf-8")
        launcher = (ROOT / "RUN_RFID_KPP_FINAL.cmd").read_text(encoding="ascii")
        for port in (18101, 18102, 18103, 18104, 18105):
            self.assertIn(str(port), waiter)
        self.assertIn("/health/ready", waiter)
        self.assertIn("wait_for_services_v3.py", launcher)
        self.assertIn("goto :startup_not_ready", launcher)

    def test_warehouse_recent_activity_has_dt_leading_index(self) -> None:
        migration = (ROOT / "migrations" / "002_resilience_indexes.sql").read_text(encoding="utf-8")
        runner = (ROOT / "deploy" / "apply_migration_v3.py").read_text(encoding="utf-8")
        self.assertIn("IX_Warehouse_Dt_Id", migration)
        self.assertIn("ON dbo.Warehouse(Dt, Id)", migration)
        self.assertIn("002_resilience_indexes.sql", runner)
        self.assertIn("IX_Warehouse_Dt_Id", runner)


if __name__ == "__main__":
    unittest.main()
