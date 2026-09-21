#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Continuous recovery layer for Perimeter RFID reader.

This adapter keeps the mature telemetry/spool instrumentation from
``monitored_rfid.py`` and fixes the semantic recovery path:

* confirmed RFID silence stays latched/red until a fresh RFID row appears;
* recovery never terminates permanently after a fixed number of attempts;
* session recovery is handed to the real reader loop, so the core cleanup path
  executes UHFStopGet/TCPDisconnect before reconnecting;
* every Nth semantic recovery gracefully recycles the Python/DLL process;
* UHFInventory start failures are handled inside the long-running process and
  cannot create a five-second wrapper restart storm;
* no speculative UHFStopGet is issued immediately before UHFInventory;
* the RFID TCP dependency reflects the SDK connection lifecycle rather than a
  competing second socket probe (important for single-client readers);
* a native SDK call that never returns is still protected by the legacy hard
  SDK watchdog.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
import time
import uuid
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
_LegacyLoadApp = legacy._load_app


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
    """Persist retry timing so process recycle cannot create a restart storm."""

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
            # Fault latch lives in a separate fail-closed state file. Corrupt
            # retry timing must not suppress recovery or turn health green.
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
        self.reporter.set_metric("self_heal_exhausted", False)
        self.reporter.set_metric(
            "self_heal_fast_attempts_exhausted",
            bool(self.state.fault_latched and attempts >= self.max_restarts),
        )
        self.reporter.set_metric("self_heal_continues", bool(self.state.fault_latched))
        self.reporter.set_metric("recovery_request_pending", _RECOVERY.pending())
        self.reporter.set_metric("recovery_last_mode", self.meta.last_mode)
        self.reporter.set_metric("recovery_last_attempt", int(self.meta.last_attempt))
        self.reporter.set_metric(
            "recovery_last_attempt_age_seconds",
            self.meta.seconds_since_attempt(),
        )

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
                        # Existing latched faults from older releases must not sit
                        # idle for another full backoff period after upgrade.
                        if self.state.fault_latched and self.state.restart_attempts >= self.max_restarts:
                            due = True
                        else:
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

    def TCPConnect(self, *args):
        rc = super().TCPConnect(*args)
        if int(rc) == 0:
            self._reporter.touch_dependency("rfid_tcp")
        else:
            self._reporter.set_dependency(
                "rfid_tcp",
                "unavailable",
                detail=f"connect_code_{int(rc)}",
            )
        return rc

    def TCPDisconnect(self, *args):
        try:
            return super().TCPDisconnect(*args)
        finally:
            self._reporter.set_dependency(
                "rfid_tcp",
                "unavailable",
                detail="disconnected",
            )

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
            exc = RuntimeError(f"RfidGracefulProcessRecycle:{attempt}:{reason}")
            self._reporter.mark_fatal(exc)
            flush_sentry(2.0)
            # SystemExit is not caught by the core's ``except Exception`` but
            # all finally blocks still execute before the wrapper restarts it.
            raise SystemExit(72)

        # RuntimeError is caught by the connection-epoch handler; its finally
        # executes UHFStopGet + TCPDisconnect, then the outer loop reconnects.
        raise RuntimeError(f"RfidControlledReconnect:{attempt}:{reason}")


def _start_inventory_or_recover(app, lib) -> bool:
    """Start inventory without ever escaping into the CMD restart loop."""
    try:
        lib.UHFInventory()
        return True
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        # SystemExit from semantic process recycle must propagate after cleanup.
        if isinstance(exc, SystemExit):
            raise
        log.error("RFID inventory start failed; resetting session: %s", exc)
        try:
            app.disconnect(lib)
        except Exception:
            pass
        time.sleep(max(0.5, float(app.Config.RECONNECT_SEC)))
        return False


def _robust_reader_main(app) -> int:
    """Production reader loop with inventory-start failure containment."""
    instance_lock = app.SingleInstanceLock(app.Config.LOCK_PATH)
    app.validate()
    lib = app.load_library()
    spool = app.Spool(app.Config.SPOOL_PATH)
    writer = app.SQLWriter(spool)
    writer.start()
    sequence = 0
    reads_total = 0
    reads_since_heartbeat = 0
    last_read_at: Optional[datetime] = None
    last_heartbeat = time.monotonic()

    try:
        while True:
            epoch = str(uuid.uuid4())
            reconnect_at = datetime.now()
            rc = lib.TCPConnect(app.Config.READER_IP.encode("utf-8"), app.Config.READER_PORT)
            if int(rc) != 0:
                log.error("TCPConnect code=%s", rc)
                time.sleep(app.Config.RECONNECT_SEC)
                continue

            if not _start_inventory_or_recover(app, lib):
                continue

            log.info("RFID reader connected, epoch=%s", epoch)
            try:
                while True:
                    length = ctypes.c_int(0)
                    buf = (ctypes.c_ubyte * 512)()
                    result = lib.UHF_GetReceived_EX(ctypes.byref(length), ctypes.byref(buf))
                    if result == 0 and length.value > 0:
                        parsed = app.parse_buf(buf, length.value)
                        if parsed:
                            sequence += 1
                            reads_total += 1
                            reads_since_heartbeat += 1
                            source_time = datetime.now()
                            last_read_at = source_time
                            quality = (
                                "APPROXIMATE_AFTER_RECONNECT"
                                if (source_time - reconnect_at).total_seconds() < app.Config.RECONNECT_UNCERTAIN_SEC
                                else "HOST_CAPTURE_TIME"
                            )
                            item = {
                                "client_uuid": str(uuid.uuid4()),
                                "source_time": source_time.isoformat(),
                                "source_sequence": sequence,
                                "connection_epoch": epoch,
                                "antenna": int(parsed["antenna"]),
                                "rssi": float(parsed["rssi"]),
                                "epc": str(parsed["epc"]),
                                "tid": str(parsed["tid"]),
                                "time_quality": quality,
                            }
                            app.enqueue_with_backpressure(spool, item)
                            if app.Config.CONSOLE:
                                print(
                                    f"{source_time:%H:%M:%S.%f}"[:-3],
                                    parsed["antenna"],
                                    f"{parsed['rssi']:.1f}",
                                    parsed["epc"],
                                    "✓ spool",
                                    flush=True,
                                )
                    else:
                        time.sleep(app.Config.IDLE_SLEEP_SEC)

                    now_mono = time.monotonic()
                    if now_mono - last_heartbeat >= app.Config.HEARTBEAT_SEC:
                        pending, sent, total = spool.stats()
                        age = (
                            "нет чтений"
                            if last_read_at is None
                            else f"{(datetime.now() - last_read_at).total_seconds():.1f}с назад"
                        )
                        db_age = (
                            "никогда"
                            if writer.last_success_at is None
                            else f"{(datetime.now() - writer.last_success_at).total_seconds():.1f}с назад"
                        )
                        log.info(
                            "STATUS reader=CONNECTED reads_total=%s reads_%ss=%s last_read=%s "
                            "spool_pending=%s spool_sent=%s spool_total=%s db_delivered=%s "
                            "db_last_ok=%s db_errors=%s",
                            reads_total,
                            int(app.Config.HEARTBEAT_SEC),
                            reads_since_heartbeat,
                            age,
                            pending,
                            sent,
                            total,
                            writer.delivered_total,
                            db_age,
                            writer.failed_total,
                        )
                        reads_since_heartbeat = 0
                        last_heartbeat = now_mono
                    time.sleep(app.Config.READ_SLEEP_SEC)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("RFID connection epoch failed")
            finally:
                app.disconnect(lib)
                time.sleep(app.Config.RECONNECT_SEC)
    except KeyboardInterrupt:
        log.info("Остановка")
    finally:
        writer.stop()
        writer.join(timeout=5)

    return 0


def _load_app_with_robust_main():
    app = _LegacyLoadApp()
    app.main = lambda: _robust_reader_main(app)
    return app


def _disable_competing_tcp_probe(reporter) -> None:
    """Remove second-socket probe; SDK TCPConnect owns this dependency."""
    try:
        with reporter.lock:
            reporter.probes.pop("rfid_tcp", None)
        reporter.set_dependency("rfid_tcp", "unknown", detail="waiting_for_sdk_connect")
    except Exception:
        # Unit-test NullReporter and future reporter implementations need not
        # expose internals; absence of a probe is then harmless.
        pass


def main() -> int:
    reporter = legacy.get_reporter()
    _disable_competing_tcp_probe(reporter)

    # Preserve legacy telemetry/spool instrumentation, replacing only recovery,
    # SDK transport health ownership and the connection-loop failure boundary.
    legacy._BusinessFlowWatchdog = _BusinessFlowWatchdog
    legacy._LibraryProxy = _LibraryProxy
    legacy._load_app = _load_app_with_robust_main
    return int(legacy.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
