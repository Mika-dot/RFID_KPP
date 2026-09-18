#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telemetry/self-heal adapter for the current RFID reader business logic."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "RFID_reader_v4"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from common.business_flow import assess_rfid_flow, source_marker  # noqa: E402
from common.observability import flush_sentry, get_reporter, safe_error_name  # noqa: E402


def _load_app():
    path = SERVICE_DIR / "rfid_to_sql_v4.py"
    spec = importlib.util.spec_from_file_location("perimeter_current_rfid", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("RfidModuleLoadFailed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _PersistentFlowState:
    """Latch confirmed silent failures across process restarts.

    A restart is not proof of recovery. The latch is cleared only after a new
    *fresh* raw RFID timestamp appears in SQL. Delayed spool/backlog delivery
    cannot make a still-dead live reader look recovered.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.fault_latched = False
        self.rfid_marker = ""
        self.restart_attempts = 0
        self.last_fault_detail = ""
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.fault_latched = bool(raw.get("fault_latched", False))
            self.rfid_marker = str(raw.get("rfid_marker", ""))
            self.restart_attempts = max(0, int(raw.get("restart_attempts", 0)))
            self.last_fault_detail = str(raw.get("last_fault_detail", ""))
        except FileNotFoundError:
            return
        except Exception:
            # Corrupt recovery metadata must never prevent the RFID service from
            # starting. The semantic watchdog will reconstruct state.
            self.fault_latched = False
            self.rfid_marker = ""
            self.restart_attempts = 0
            self.last_fault_detail = "state_file_invalid"

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fault_latched": self.fault_latched,
            "rfid_marker": self.rfid_marker,
            "restart_attempts": self.restart_attempts,
            "last_fault_detail": self.last_fault_detail,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        last_error: Optional[BaseException] = None
        for attempt in range(5):
            temp = self.path.with_name(
                f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.{attempt}.tmp"
            )
            try:
                temp.write_text(raw, encoding="utf-8")
                os.replace(str(temp), str(self.path))
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
            finally:
                try:
                    if temp.exists():
                        temp.unlink()
                except OSError:
                    pass
        if last_error is not None:
            raise last_error
        raise RuntimeError("RfidBusinessStateWriteFailed")

    def latch(self, marker: str, detail: str) -> None:
        with self.lock:
            if not self.fault_latched:
                self.restart_attempts = 0
            self.fault_latched = True
            self.rfid_marker = marker
            self.last_fault_detail = detail
            self._save()

    def clear(self) -> None:
        with self.lock:
            self.fault_latched = False
            self.rfid_marker = ""
            self.restart_attempts = 0
            self.last_fault_detail = ""
            self._save()

    def register_restart(self) -> int:
        with self.lock:
            self.restart_attempts += 1
            self._save()
            return self.restart_attempts


class _BusinessFlowWatchdog:
    def __init__(self, reporter, connection_string: str) -> None:
        self.reporter = reporter
        self.connection_string = connection_string
        self.interval_sec = max(5.0, float(os.getenv("RFID_BUSINESS_CHECK_SEC", "15")))
        self.stall_sec = max(60.0, float(os.getenv("RFID_BUSINESS_STALL_SEC", "900")))
        self.evidence_window_sec = max(
            self.stall_sec,
            float(os.getenv("RFID_BUSINESS_EVIDENCE_WINDOW_SEC", "1800")),
        )
        self.self_heal_sec = max(15.0, float(os.getenv("RFID_BUSINESS_SELF_HEAL_SEC", "60")))
        self.max_restarts = max(0, int(os.getenv("RFID_BUSINESS_MAX_RESTARTS", "3")))
        self.min_video_events = max(1, int(os.getenv("RFID_BUSINESS_MIN_VIDEO_EVENTS", "2")))
        state_path = Path(
            os.getenv(
                "RFID_BUSINESS_STATE_PATH",
                str(ROOT / "runtime" / "rfid_business_flow_state.json"),
            )
        )
        self.state = _PersistentFlowState(state_path)
        self.unavailable_since: Optional[float] = None
        self.last_reported_state = ""
        self.exhausted_reported = False
        threading.Thread(target=self._loop, name="rfid-business-flow", daemon=True).start()

    def _query_snapshot(self):
        import pyodbc

        conn = None
        cur = None
        try:
            conn = pyodbc.connect(self.connection_string, autocommit=True, timeout=8)
            cur = conn.cursor()
            cur.execute("SELECT SYSDATETIME()")
            now = cur.fetchone()[0]

            cur.execute(
                """
SELECT TOP(1) COALESCE(SourceReaderTime,ReceivedAt,RecordTime)
FROM dbo.RFID_Tags
ORDER BY Id DESC;
"""
            )
            row = cur.fetchone()
            rfid_at = row[0] if row else None

            cur.execute(
                """
SELECT TOP(1) COALESCE(CapturedAt,[Timestamp])
FROM dbo.ReelTransitions
ORDER BY Id DESC;
"""
            )
            row = cur.fetchone()
            video_at = row[0] if row else None
            cur.execute(
                """
SELECT COUNT_BIG(*)
FROM dbo.ReelTransitions
WHERE (CapturedAt >= DATEADD(SECOND,?,SYSDATETIME()))
   OR (CapturedAt IS NULL AND [Timestamp] >= DATEADD(SECOND,?,SYSDATETIME()));
""",
                -int(self.evidence_window_sec),
                -int(self.evidence_window_sec),
            )
            video_recent_events = int(cur.fetchone()[0] or 0)

            cur.execute("SELECT TOP(1) Dt FROM dbo.Warehouse ORDER BY Id DESC;")
            row = cur.fetchone()
            warehouse_at = row[0] if row else None
            cur.execute(
                """
SELECT COUNT_BIG(*)
FROM dbo.Warehouse
WHERE Dt >= DATEADD(SECOND,?,SYSDATETIME());
""",
                -int(self.evidence_window_sec),
            )
            warehouse_recent_events = int(cur.fetchone()[0] or 0)
            return (
                now,
                rfid_at,
                video_at,
                warehouse_at,
                video_recent_events,
                warehouse_recent_events,
            )
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _publish_metrics(self, assessment, video_recent_events: int, warehouse_recent_events: int) -> None:
        self.reporter.set_metric("rfid_source_age_seconds", assessment.rfid_age_seconds)
        self.reporter.set_metric("video_source_age_seconds", assessment.video_age_seconds)
        self.reporter.set_metric("warehouse_source_age_seconds", assessment.warehouse_age_seconds)
        self.reporter.set_metric("video_recent_events", int(video_recent_events))
        self.reporter.set_metric("warehouse_recent_events", int(warehouse_recent_events))
        self.reporter.set_metric("business_flow_latched", bool(self.state.fault_latched))
        self.reporter.set_metric("self_heal_restarts", int(self.state.restart_attempts))
        self.reporter.set_metric(
            "self_heal_exhausted",
            bool(self.state.fault_latched and self.state.restart_attempts >= self.max_restarts),
        )

    def _loop(self) -> None:
        while True:
            try:
                (
                    now,
                    rfid_at,
                    video_at,
                    warehouse_at,
                    video_recent_events,
                    warehouse_recent_events,
                ) = self._query_snapshot()
                assessment = assess_rfid_flow(
                    now=now,
                    rfid_at=rfid_at,
                    video_at=video_at,
                    warehouse_at=warehouse_at,
                    video_recent_events=video_recent_events,
                    warehouse_recent_events=warehouse_recent_events,
                    stall_seconds=self.stall_sec,
                    min_video_events=self.min_video_events,
                    fault_latched=self.state.fault_latched,
                    latched_rfid_marker=self.state.rfid_marker,
                )

                if assessment.clear_latch:
                    self.state.clear()
                    self.exhausted_reported = False
                elif assessment.latch_fault:
                    self.state.latch(source_marker(rfid_at), assessment.detail)

                self._publish_metrics(assessment, video_recent_events, warehouse_recent_events)
                self.reporter.set_dependency(
                    "business_flow",
                    assessment.status,
                    data_age_seconds=assessment.rfid_age_seconds,
                    stale_after_seconds=max(30.0, self.interval_sec * 3.0),
                    detail=assessment.detail,
                )

                if (
                    assessment.status != self.last_reported_state
                    and assessment.status in {"degraded", "unavailable"}
                ):
                    self.reporter.capture_exception(RuntimeError(f"RfidBusinessFlow:{assessment.detail}"))
                    flush_sentry(2.0)
                self.last_reported_state = assessment.status

                if assessment.status == "unavailable":
                    if self.state.restart_attempts >= self.max_restarts:
                        self.unavailable_since = None
                        if not self.exhausted_reported:
                            self.reporter.capture_exception(
                                RuntimeError("RfidBusinessFlowSelfHealExhausted")
                            )
                            flush_sentry(2.0)
                            self.exhausted_reported = True
                    else:
                        if self.unavailable_since is None:
                            self.unavailable_since = time.monotonic()
                        elif time.monotonic() - self.unavailable_since >= self.self_heal_sec:
                            attempt = self.state.register_restart()
                            self.reporter.set_metric("self_heal_restarts", attempt)
                            exc = RuntimeError(f"RfidBusinessFlowSelfHealRestart:{attempt}")
                            self.reporter.mark_fatal(exc)
                            flush_sentry(2.0)
                            os._exit(72)
                else:
                    self.unavailable_since = None
            except Exception as exc:
                # A semantic-probe failure must never produce a false green.
                # SQL availability is handled independently, so do not restart
                # the physical reader solely because this diagnostic query failed.
                self.unavailable_since = None
                self.reporter.set_dependency(
                    "business_flow",
                    "degraded",
                    stale_after_seconds=max(30.0, self.interval_sec * 3.0),
                    detail=f"probe_{safe_error_name(exc)}",
                )
                if self.last_reported_state != "probe_error":
                    self.reporter.capture_exception(exc)
                self.last_reported_state = "probe_error"
            time.sleep(self.interval_sec)


class _SdkCallWatchdog:
    def __init__(self, reporter, timeout_sec: float) -> None:
        self.reporter = reporter
        self.timeout_sec = max(5.0, float(timeout_sec))
        self.lock = threading.Lock()
        self.active_name = ""
        self.active_since = 0.0
        threading.Thread(target=self._loop, name="rfid-sdk-monitor", daemon=True).start()

    def call(self, name: str, func, *args):
        with self.lock:
            self.active_name = name
            self.active_since = time.monotonic()
        try:
            return func(*args)
        finally:
            with self.lock:
                self.active_name = ""
                self.active_since = 0.0

    def _loop(self) -> None:
        while True:
            time.sleep(0.5)
            with self.lock:
                name = self.active_name
                age = time.monotonic() - self.active_since if name else 0.0
            if not name or age < self.timeout_sec:
                continue
            self.reporter.set_dependency(
                "rfid_reader",
                "unavailable",
                data_age_seconds=age,
                detail="sdk_call_timeout",
            )
            exc = RuntimeError(f"RfidSdkCallTimeout:{name}")
            self.reporter.mark_fatal(exc)
            flush_sentry(2.0)
            os._exit(70)


class _LibraryProxy:
    def __init__(self, lib, reporter, watchdog, no_data_codes: set[int]) -> None:
        self._lib = lib
        self._reporter = reporter
        self._watchdog = watchdog
        self._no_data_codes = no_data_codes
        self._receive_error_streak = 0
        self._receive_error_limit = max(1, int(os.getenv("RFID_SDK_ERROR_STREAK_LIMIT", "5")))

    def __getattr__(self, name):
        return getattr(self._lib, name)

    def _loop_progress(self) -> None:
        self._reporter.progress("reader_loop")

    def TCPConnect(self, *args):
        try:
            rc = self._watchdog.call("TCPConnect", self._lib.TCPConnect, *args)
        except BaseException as exc:
            self._reporter.set_dependency("rfid_reader", "unavailable", detail=safe_error_name(exc))
            raise
        self._loop_progress()
        if int(rc) == 0:
            self._reporter.touch_dependency("rfid_reader")
        else:
            self._reporter.set_dependency("rfid_reader", "unavailable", detail=f"connect_code_{int(rc)}")
        return rc

    def UHFInventory(self, *args):
        rc = self._watchdog.call("UHFInventory", self._lib.UHFInventory, *args)
        self._loop_progress()
        code = int(rc)
        if code != 0:
            self._reporter.set_dependency(
                "rfid_reader",
                "unavailable",
                detail=f"inventory_code_{code}",
            )
            raise RuntimeError(f"RfidInventoryStartFailed:{code}")
        self._receive_error_streak = 0
        self._reporter.touch_dependency("rfid_reader")
        return rc

    def UHF_GetReceived_EX(self, *args):
        try:
            rc = self._watchdog.call("UHF_GetReceived_EX", self._lib.UHF_GetReceived_EX, *args)
        except BaseException as exc:
            self._reporter.set_dependency("rfid_reader", "unavailable", detail=safe_error_name(exc))
            raise
        self._loop_progress()
        code = int(rc)
        if code == 0 or code in self._no_data_codes:
            self._receive_error_streak = 0
            self._reporter.touch_dependency("rfid_reader")
            return rc

        self._receive_error_streak += 1
        self._reporter.set_dependency(
            "rfid_reader",
            "unavailable",
            detail=f"receive_code_{code}",
        )
        if self._receive_error_streak >= self._receive_error_limit:
            raise RuntimeError(f"RfidReceiveErrorStreak:{code}")
        return rc

    def UHFStopGet(self, *args):
        rc = self._watchdog.call("UHFStopGet", self._lib.UHFStopGet, *args)
        self._loop_progress()
        return rc

    def TCPDisconnect(self, *args):
        try:
            return self._watchdog.call("TCPDisconnect", self._lib.TCPDisconnect, *args)
        finally:
            self._loop_progress()
            self._reporter.set_dependency("rfid_reader", "unavailable", detail="disconnected")


def main() -> int:
    app = _load_app()
    reporter = get_reporter()

    for dependency in ("reader_loop", "delivery_writer"):
        if dependency not in reporter.required_dependencies:
            reporter.required_dependencies = tuple(reporter.required_dependencies) + (dependency,)

    reporter.register_progress_watchdog(
        "reader_loop",
        timeout_seconds=float(os.getenv("RFID_READER_LOOP_WATCHDOG_SEC", "90")),
        exit_code=75,
    )
    reporter.register_progress_watchdog(
        "delivery_writer",
        timeout_seconds=float(os.getenv("RFID_DELIVERY_WRITER_WATCHDOG_SEC", "90")),
        exit_code=76,
    )

    watchdog = _SdkCallWatchdog(
        reporter,
        float(os.getenv("RFID_SDK_CALL_TIMEOUT_SEC", "30")),
    )
    no_data_codes: set[int] = set()
    for raw in os.getenv("RFID_SDK_NO_DATA_CODES", "-1").split(","):
        try:
            no_data_codes.add(int(raw.strip()))
        except ValueError:
            pass

    original_load_library = app.load_library

    def monitored_load_library():
        lib = original_load_library()
        return _LibraryProxy(lib, reporter, watchdog, no_data_codes)

    app.load_library = monitored_load_library

    # The SQL delivery thread calls Spool.next_pending every ~200 ms even when
    # idle. Its progress therefore proves both that the thread is alive and that
    # SQLite remains readable. If the thread dies or SQLite blocks indefinitely,
    # the delivery_writer watchdog rebuilds the process.
    original_next_pending = app.Spool.next_pending

    def monitored_next_pending(spool):
        try:
            result = original_next_pending(spool)
            reporter.progress("delivery_writer")
            return result
        except Exception as exc:
            reporter.set_dependency(
                "delivery_writer",
                "unavailable",
                detail=safe_error_name(exc),
            )
            reporter.capture_exception(exc)
            raise

    app.Spool.next_pending = monitored_next_pending

    _BusinessFlowWatchdog(reporter, app.Config.DB_CONN)
    return int(app.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
