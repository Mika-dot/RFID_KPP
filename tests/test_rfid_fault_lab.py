from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common.business_flow import assess_rfid_flow, source_marker
from deploy import monitored_rfid_recovery_v2 as production


ROOT = Path(__file__).resolve().parents[1]


class _State:
    def __init__(self, attempts: int = 0) -> None:
        self.restart_attempts = attempts

    def register_restart(self) -> int:
        self.restart_attempts += 1
        return self.restart_attempts


class _Meta:
    def __init__(self) -> None:
        self.records = []

    def record(self, attempt: int, mode: str) -> None:
        self.records.append((attempt, mode))


class _Reporter:
    def __init__(self) -> None:
        self.metrics = {}
        self.exceptions = []

    def set_metric(self, name, value) -> None:
        self.metrics[name] = value

    def capture_exception(self, exc) -> None:
        self.exceptions.append(type(exc).__name__)


class _RecoveryController:
    def __init__(self) -> None:
        self.requests = []

    def request(self, *, attempt: int, reason: str, mode: str) -> bool:
        self.requests.append((attempt, reason, mode))
        return True


class RfidFaultLabTests(unittest.TestCase):
    def test_randomized_business_flow_invariants(self) -> None:
        """20k deterministic snapshots: no impossible latch/clear combinations."""
        rng = random.Random(0xBADC0DE)
        now = datetime(2026, 9, 21, 10, 0, 0)
        stall = 900.0

        for _ in range(20_000):
            rfid_age = rng.choice([None, rng.uniform(0, 3000)])
            video_age = rng.choice([None, rng.uniform(0, 3000)])
            warehouse_age = rng.choice([None, rng.uniform(0, 3000)])
            video_events = rng.randrange(0, 6)
            warehouse_events = rng.randrange(0, 4)
            latched = rng.choice([False, True])

            rfid_at = None if rfid_age is None else now - timedelta(seconds=rfid_age)
            video_at = None if video_age is None else now - timedelta(seconds=video_age)
            warehouse_at = None if warehouse_age is None else now - timedelta(seconds=warehouse_age)

            # Half of latched samples use the same marker, half emulate a new row.
            marker = source_marker(rfid_at)
            if latched and rng.random() < 0.5:
                latched_marker = marker
            else:
                latched_marker = "older-marker"

            a = assess_rfid_flow(
                now=now,
                rfid_at=rfid_at,
                video_at=video_at,
                warehouse_at=warehouse_at,
                video_recent_events=video_events,
                warehouse_recent_events=warehouse_events,
                stall_seconds=stall,
                min_video_events=2,
                fault_latched=latched,
                latched_rfid_marker=latched_marker,
            )

            self.assertFalse(a.latch_fault and a.clear_latch)
            if a.clear_latch:
                self.assertTrue(latched)
                self.assertIsNotNone(rfid_at)
                self.assertLessEqual(a.rfid_age_seconds, stall)
                self.assertNotEqual(marker, latched_marker)
                self.assertEqual(a.status, "ok")
            if latched and not a.clear_latch:
                self.assertEqual(a.status, "unavailable")

    def test_idle_checkpoint_never_triggers_recovery_evidence(self) -> None:
        now = datetime(2026, 9, 21, 10, 0, 0)
        a = assess_rfid_flow(
            now=now,
            rfid_at=now - timedelta(days=10),
            video_at=None,
            warehouse_at=None,
            video_recent_events=0,
            warehouse_recent_events=0,
            stall_seconds=900,
            min_video_events=2,
        )
        self.assertEqual(a.status, "ok")
        self.assertFalse(a.latch_fault)
        self.assertEqual(a.detail, "checkpoint_idle_no_activity_evidence")

    def test_cross_source_activity_latches_silent_rfid(self) -> None:
        now = datetime(2026, 9, 21, 10, 0, 0)
        for video_events, warehouse_events in ((2, 0), (1, 1), (5, 2)):
            with self.subTest(video=video_events, warehouse=warehouse_events):
                a = assess_rfid_flow(
                    now=now,
                    rfid_at=now - timedelta(days=1),
                    video_at=now - timedelta(seconds=20),
                    warehouse_at=now - timedelta(seconds=30),
                    video_recent_events=video_events,
                    warehouse_recent_events=warehouse_events,
                    stall_seconds=900,
                    min_video_events=2,
                )
                self.assertEqual(a.status, "unavailable")
                self.assertTrue(a.latch_fault)

    def test_recovery_backoff_is_bounded_and_non_thrashing(self) -> None:
        wd = production._BusinessFlowWatchdog.__new__(production._BusinessFlowWatchdog)
        wd.self_heal_sec = 60.0
        wd.max_restarts = 3
        wd.recovery_backoff_sec = 300.0
        wd.recovery_backoff_max_sec = 1800.0
        wd.state = _State()

        expected = {
            0: 60.0,
            1: 60.0,
            2: 60.0,
            3: 300.0,
            4: 600.0,
            5: 1200.0,
            6: 1800.0,
            20: 1800.0,
        }
        for attempts, delay in expected.items():
            wd.state.restart_attempts = attempts
            self.assertEqual(wd._recovery_delay(), delay)

    def test_process_recycle_occurs_once_per_incident_not_periodically(self) -> None:
        modes = []
        for existing_attempts in range(0, 8):
            wd = production._BusinessFlowWatchdog.__new__(production._BusinessFlowWatchdog)
            wd.state = _State(existing_attempts)
            wd.meta = _Meta()
            wd.reporter = _Reporter()
            wd.process_recycle_attempt = 3
            controller = _RecoveryController()
            with mock.patch.object(production.base, "_RECOVERY", controller), mock.patch.object(
                production.base, "flush_sentry", return_value=None
            ):
                wd._request_recovery("fault")
            self.assertEqual(len(controller.requests), 1)
            modes.append(controller.requests[0][2])

        self.assertEqual(modes.count("process"), 1)
        self.assertEqual(modes[2], "process")
        self.assertTrue(all(mode == "session" for i, mode in enumerate(modes) if i != 2))

    def test_safe_restart_controller_cannot_kill_cmd_or_sibling_services(self) -> None:
        text = (ROOT / "deploy" / "restart_service_safe.ps1").read_text(encoding="utf-8")
        self.assertIn("expected exactly one Python runner", text)
        self.assertIn("^python(w)?\\.exe$", text)
        self.assertIn("run_service\\.py", text)
        self.assertIn("Stop-Process -Id $oldPid", text)
        self.assertNotIn("Stop-Process -Name", text)
        self.assertNotIn("RUN_RFID_READER_V3.cmd*", text)
        self.assertNotIn("RUN_RUSGUARD_V3.cmd*", text)
        self.assertNotIn("RUN_RTSP_V3.cmd*", text)
        self.assertNotIn("RUN_AGGREGATOR_V3.cmd*", text)
        self.assertNotIn("RUN_WEB_V3.cmd*", text)

    def test_status_script_is_strictly_read_only(self) -> None:
        text = (ROOT / "deploy" / "status_all_services.ps1").read_text(encoding="utf-8")
        self.assertNotIn("Stop-Process", text)
        self.assertNotIn("Start-Process", text)
        self.assertNotIn("taskkill", text.lower())
        for port in range(18101, 18106):
            self.assertIn(str(port), text)


if __name__ == "__main__":
    unittest.main()
