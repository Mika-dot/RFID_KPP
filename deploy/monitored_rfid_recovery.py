#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Continuous recovery layer for Perimeter RFID reader.

The legacy monitored adapter correctly detects semantic RFID silence, but its
old recovery path used os._exit(72) and permanently stopped retrying after the
configured fast-attempt budget.  This layer keeps the same health contract and
adds a safe recovery state machine:

* confirmed semantic failure stays latched/red until a fresh RFID read exists;
* retries never stop permanently;
* normal recovery is requested from the reader loop so the core finally block
  executes UHFStopGet() and TCPDisconnect() before reconnecting;
* every Nth recovery performs a graceful whole-process recycle via SystemExit,
  which still executes the core cleanup/finally blocks;
* a stuck native SDK call remains protected by the legacy hard watchdog.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.business_flow import assess_rfid_flow, source_marker  # noqa: E402
from common.observability import flush_sentry, safe_error_name  # noqa: E402
import deploy.monitored_rfid as legacy  # noqa: E402

log = logging.getLogger("perimeter-rfid-recovery")

_LegacyBusinessFlowWatchdog = legacy._BusinessFlowWatchdog
_LegacyLibraryProxy = legacy._LibraryProxy


class _RecoveryController:
    """Thread-safe handoff from semantic watchdog to the actual reader loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Optional[dict] = None

    def request(self, *, attempt: int, reason: str, mode: str) -> bool:
        with self._lock:
            if self._pending is not None:
                return False
            self._pending = {
                "attempt": int(attempt),
                "reason": str(reason),
                "mode": str(mode),
                "requested_at": datetime.now().isoformat(timespec="seconds"),
            }
            return True

    def consume(self) -> Optional[dict]:
        with self._lock:
            item = self._pending
            self._pending = None
            return item

    def pending(self) -> bool:
        with self._lock:
            return self._pending is not None


class _RecoveryMeta:
    """Persist retry timing so a process recycle cannot create a restart storm."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.last_attempt_at: Optional[datetime] = None
        self.last_mode = ""
        self.last_attempt = 0
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            value = str(raw.get("last_attempt_at", "") or "")
            self.last_attempt_at = datetime.fromisoformat(value) if value else None
            self.last_mode = str(raw.get("last_mode", "") or "")
            self.last_attempt = max(0, int(raw.get("last_attempt", 0) or 0))
        except FileNotFoundError:
            return
        except Exception:
            # Corrupt retry metadata must never turn a real business-flow fault
            # green. Forget only the timer and let the latched health state drive
            # a new controlled recovery.
            self.last_attempt_at = None
            self.last_mode = "metadata_invalid"
            self.last_attempt = 0

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_attempt_at": self.last_attempt_at.isoformat(timespec="seconds") if self.last_attempt_at else "",
            "last_mode": self.last_mode,
            "last_attempt": self.last_attempt,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        last_error: Optional[BaseException] = None
        for index in range(5):
            temp = self.path.with_name(
                f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.{index}.tmp"
            )
            try:
                temp.write_text(raw, encoding="utf-8")
                os.replace(str(temp), str(self.path))
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.05 * (index + 1))
            finally:
                try:
                    if temp.exists():
                        temp.unlink()
                except OSError:
                    pass
        if last_error is not None:
            raise last_error

    def record(self, attempt: int, mode: str) -> None:
        with self.lock:
            self.last_attempt_at = datetime.now()
            self.last_mode = str(mode)
            self.last_attempt = int(attempt)
            self._save()

    def clear_active_timer(self) -> None:
        with self.lock:
            self.last_attempt_at = None
            self.last_mode = "recovered"
            self.last_attempt = 0
            self._save()

    def seconds_since_attempt(self) -> Optional[float]:
        with self.lock:
            if self.last_attempt_at is None:
                return None
            return max(0.0, (datetime.now() - self.last_attempt_at).total_seconds())


_RECOVERY = _RecoveryController()


class _BusinessFlowWatchdog(_LegacyBusinessFlowWatchdog):
    """Semantic watchdog with continuous, bounded-rate recovery."""

    def __init__(self, reporter, connection_string: str) -> None:
        self.recovery_backoff_sec = max(
            60.0,
            float(os.getenv("RFID_BUSINESS_RECOVERY_BACKOFF_SEC", "300")),
        )
        self.process_recycle_every = max(
            0,
            int(os.getenv("RFID_BUSINESS_PROCESS_RECYCLE_EVERY", "3")),
        )
        self.meta = _RecoveryMeta(
            Path(
                os.getenv(
                    "RFID_BUSINESS_RECOVERY_META_PATH",
                    str(ROOT / "runtime" / "rfid_business_recovery_meta.json"),
                )
            )
        )
        super().__init__(reporter, connection_string)

    def _publish_metrics(self, assessment, video_recent_events: int, warehouse_recent_events: int) -> None:
        super()._publish_metrics(assessment, video_recent_events, warehouse_recent_events)
        attempts = int(self.state.restart_attempts)
        # Compatibility metric: recovery no longer has a terminal exhausted
        # state.  After the fast-attempt budget it switches to bounded backoff.
        self.reporter.set_metric("self_heal_exhausted", False)
        self.reporter.set_metric(
            "self_heal_fast_attempts_exhausted",
            bool(self.state.fault_latched and attempts >= self.max_restarts),
        )
        self.reporter.set_metric("self_heal_continues", bool(self.state.fault_latched))
        self.reporter.set_metric("recovery_request_pending", _RECOVERY.pending())
        self.reporter.set_metric("recovery_last_mode", self.meta.last_mode)
        self.reporter.set_metric("recovery_last_attempt", int(self.meta.last_attempt))
        age = self.meta.seconds_since_attempt()
        self.reporter.set_metric("recovery_last_attempt_age_seconds", age)

    def _recovery_delay(self) -> float:
        if int(self.state.restart_attempts) < int(self.max_restarts):
            return self.self_heal_sec
        return self.recovery_backoff_sec

    def _request_recovery(self, detail: str) -> None:
        next_attempt = int(self.state.restart_attempts) + 1
        mode = "session"
        if self.process_recycle_every > 0 and next_attempt % self.process_recycle_every == 0:
            mode = "process"
        if not _RECOVERY.request(attempt=next_attempt, reason=detail, mode=mode):
            return
        attempt = self.state.register_restart()
        self.meta.record(attempt, mode)
        self.reporter.set_metric("self_heal_restarts", attempt)
        self.reporter.set_metric("recovery_request_pending", True)
        self.reporter.set_metric("recovery_last_mode", mode)
        self.reporter.set_metric("recovery_last_attempt", attempt)
        self.reporter.capture_exception(
            RuntimeError(f"RfidBusinessFlowControlledRecovery:{mode}:{attempt}:{detail}")
        )
        flush_sentry(2.0)
        log.error(
            "RFID semantic failure: requested %s recovery attempt=%s detail=%s",
            mode,
            attempt,
            detail,
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
                    self.meta.clear_active_timer()
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
                    self.reporter.capture_exception(
                        RuntimeError(f"RfidBusinessFlow:{assessment.detail}")
                    )
                    flush_sentry(2.0)
                self.last_reported_state = assessment.status

                if assessment.status == "unavailable":
                    if self.unavailable_since is None:
                        self.unavailable_since = time.monotonic()

                    since_attempt = self.meta.seconds_since_attempt()
                    if since_attempt is None:
                        due = (time.monotonic() - self.unavailable_since) >= self.self_heal_sec
                    else:
                        due = since_attempt >= self._recovery_delay()

                    if due and not _RECOVERY.pending():
                        self._request_recovery(assessment.detail)
                        self.unavailable_since = time.monotonic()
                else:
                    self.unavailable_since = None

            except Exception as exc:
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


class _LibraryProxy(_LegacyLibraryProxy):
    """Reader proxy that performs recovery on the reader-loop thread."""

    def UHFInventory(self, *args):
        # A process/TCP reconnect is not enough for some reader firmware states.
        # Explicitly stop any stale inventory session before starting a new one.
        try:
            super().UHFStopGet()
        except Exception as exc:
            self._reporter.capture_exception(exc)
            self._reporter.set_dependency(
                "rfid_reader",
                "degraded",
                detail=f"pre_inventory_stop_{safe_error_name(exc)}",
            )
        settle = max(0.0, float(os.getenv("RFID_INVENTORY_RESET_SETTLE_SEC", "0.25")))
        if settle:
            time.sleep(settle)
        return super().UHFInventory(*args)

    def UHF_GetReceived_EX(self, *args):
        rc = super().UHF_GetReceived_EX(*args)
        recovery = _RECOVERY.consume()
        if recovery is None:
            return rc

        attempt = int(recovery["attempt"])
        mode = str(recovery["mode"])
        reason = str(recovery["reason"])
        self._reporter.set_metric("recovery_request_pending", False)
        self._reporter.set_dependency(
            "rfid_reader",
            "unavailable",
            detail=f"controlled_{mode}_recovery_{attempt}",
        )

        if mode == "process":
            # SystemExit is intentionally not caught by the core's
            # ``except Exception``.  Its finally blocks still run, therefore
            # UHFStopGet/TCPDisconnect and writer shutdown happen before the
            # CMD wrapper starts a fresh Python/DLL process.
            exc = RuntimeError(f"RfidGracefulProcessRecycle:{attempt}:{reason}")
            self._reporter.mark_fatal(exc)
            flush_sentry(2.0)
            raise SystemExit(72)

        # RuntimeError *is* caught by the core connection-epoch handler.  The
        # epoch's finally block performs UHFStopGet + TCPDisconnect and the
        # outer loop then TCPConnects and starts inventory again.
        raise RuntimeError(f"RfidControlledReconnect:{attempt}:{reason}")


def main() -> int:
    # Preserve the mature legacy telemetry/spool instrumentation and replace
    # only the two pieces involved in semantic recovery.
    legacy._BusinessFlowWatchdog = _BusinessFlowWatchdog
    legacy._LibraryProxy = _LibraryProxy
    return int(legacy.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
