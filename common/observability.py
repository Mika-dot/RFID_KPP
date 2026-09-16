#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared Sentry, health and heartbeat support for Perimeter services.

The module deliberately has no dependency on Flask and uses only the Python
standard library for health endpoints.  This keeps it usable by the 32-bit
RFID reader interpreter as well as the 64-bit services.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple


DEFAULT_RELEASE = "3.7.1"
DEFAULT_HEALTH_HOST = "127.0.0.1"
DEFAULT_HEARTBEAT_SEC = 10.0
DEFAULT_STALE_SEC = 45.0

SERVICE_CONFIG: Dict[str, Dict[str, Any]] = {
    "Perimeter.RfidReader": {
        "port": 18101,
        "dsn": "https://fc21ec3c29a3445a95d483067c3b2a7d@sentry.mositlab.ru/21",
        "required": ("database", "rfid_reader"),
    },
    "Perimeter.RusGuardSync": {
        "port": 18102,
        "dsn": "https://d038ae40056b4792bce81763ea41c6a7@sentry.mositlab.ru/22",
        "required": ("source_database", "destination_database", "sync_loop"),
    },
    "Perimeter.Yolo": {
        "port": 18103,
        "dsn": "https://91da78d950f84b348e696907455d9e88@sentry.mositlab.ru/23",
        "required": ("database", "model", "camera_0", "camera_1", "pipeline"),
    },
    "Perimeter.Aggregator": {
        "port": 18104,
        "dsn": "https://cd3c8e6a79984541b4fa169649906fe1@sentry.mositlab.ru/24",
        "required": ("database", "pipeline", "rfid_reader", "yolo", "rusguard"),
    },
    "Perimeter.WebDashboard": {
        "port": 18105,
        "dsn": "https://b5377d18f33344229b834cf62127b4e1@sentry.mositlab.ru/25",
        "required": ("database", "aggregator"),
    },
}

_sentry_rate_lock = threading.Lock()
_sentry_last_event: Dict[str, float] = {}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: Optional[datetime] = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_error_name(exc: BaseException) -> str:
    """Return a non-sensitive error classifier suitable for health JSON."""
    return type(exc).__name__


def _safe_detail(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    lowered = str(value).lower()
    if any(
        marker in lowered
        for marker in ("pwd", "password", "dsn", "token", "secret", "rtsp://", "http://", "https://", "uid=")
    ):
        return "redacted"
    # Health output must never become an accidental secret/error dump.
    clean = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(value))[:80]
    return clean or None


class NullReporter:
    """No-op reporter used when active modules are imported by unit tests."""

    def set_dependency(self, *args: Any, **kwargs: Any) -> None:
        return None

    def touch_dependency(self, *args: Any, **kwargs: Any) -> None:
        return None

    def mark_success(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set_metric(self, *args: Any, **kwargs: Any) -> None:
        return None

    def register_probe(self, *args: Any, **kwargs: Any) -> None:
        return None

    def register_peer(self, *args: Any, **kwargs: Any) -> None:
        return None

    def register_progress_watchdog(self, *args: Any, **kwargs: Any) -> None:
        return None

    def progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    def capture_exception(self, *args: Any, **kwargs: Any) -> None:
        return None

    def mark_fatal(self, *args: Any, **kwargs: Any) -> None:
        return None

    def snapshot(self, ready: bool = True) -> Tuple[Dict[str, Any], int]:
        data = {
            "status": "degraded" if ready else "ok",
            "service": "unmanaged",
            "version": os.getenv("PERIMETER_RELEASE", DEFAULT_RELEASE),
            "uptime_seconds": 0,
            "last_success": None,
            "dependencies": {},
        }
        return data, 503 if ready else 200


class HealthReporter:
    def __init__(
        self,
        service: str,
        version: str,
        required_dependencies: Tuple[str, ...],
        heartbeat_path: Path,
        heartbeat_sec: float = DEFAULT_HEARTBEAT_SEC,
    ) -> None:
        self.service = service
        self.version = version
        self.required_dependencies = tuple(required_dependencies)
        self.heartbeat_path = heartbeat_path
        self.heartbeat_sec = max(2.0, min(float(heartbeat_sec), 30.0))
        self.started_wall = utc_now()
        self.started_mono = time.monotonic()
        self.last_success: Optional[datetime] = None
        self.dependencies: Dict[str, Dict[str, Any]] = {}
        self.metrics: Dict[str, Any] = {}
        self.probes: Dict[str, Tuple[Callable[[], Optional[Mapping[str, Any]]], float]] = {}
        self.peers: Dict[str, Tuple[str, float]] = {}
        self.progress_watchdogs: Dict[str, Dict[str, Any]] = {}
        self.fatal = False
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.server: Optional[ThreadingHTTPServer] = None
        for name in self.required_dependencies:
            self.set_dependency(name, "unknown")

    def set_dependency(
        self,
        name: str,
        status: str,
        *,
        latency_ms: Optional[float] = None,
        data_age_seconds: Optional[float] = None,
        last_success: Optional[datetime] = None,
        stale_after_seconds: Optional[float] = DEFAULT_STALE_SEC,
        detail: Optional[str] = None,
    ) -> None:
        normalized = status.lower().strip()
        if normalized not in {"ok", "degraded", "unavailable", "unknown"}:
            normalized = "unknown"
        now = utc_now()
        with self.lock:
            previous = self.dependencies.get(name, {})
            dep_last_success = last_success
            if normalized == "ok":
                dep_last_success = dep_last_success or now
                self.last_success = now
            elif dep_last_success is None:
                dep_last_success = previous.get("_last_success_dt")
            self.dependencies[name] = {
                "status": normalized,
                "updated_at": iso_utc(now),
                "_updated_mono": time.monotonic(),
                "last_success": iso_utc(dep_last_success) if dep_last_success else None,
                "_last_success_dt": dep_last_success,
                "latency_ms": round(float(latency_ms), 1) if latency_ms is not None else None,
                "data_age_seconds": round(max(0.0, float(data_age_seconds)), 1)
                if data_age_seconds is not None
                else None,
                # None is used for immutable dependencies such as a model that
                # is loaded once and then remains resident for the process
                # lifetime.  Dynamic dependencies always retain a timeout.
                "stale_after_seconds": None
                if stale_after_seconds is None
                else max(5.0, float(stale_after_seconds)),
                "detail": _safe_detail(detail),
            }

    def touch_dependency(
        self,
        name: str,
        *,
        data_age_seconds: Optional[float] = None,
        latency_ms: Optional[float] = None,
        stale_after_seconds: Optional[float] = DEFAULT_STALE_SEC,
    ) -> None:
        self.set_dependency(
            name,
            "ok",
            data_age_seconds=data_age_seconds,
            latency_ms=latency_ms,
            stale_after_seconds=stale_after_seconds,
        )

    def mark_success(self, when: Optional[datetime] = None) -> None:
        with self.lock:
            self.last_success = when or utc_now()

    def set_metric(self, name: str, value: Any) -> None:
        if isinstance(value, (str, int, float, bool)) or value is None:
            with self.lock:
                self.metrics[name] = value

    def register_probe(
        self,
        name: str,
        probe: Callable[[], Optional[Mapping[str, Any]]],
        *,
        stale_after_seconds: float = DEFAULT_STALE_SEC,
    ) -> None:
        with self.lock:
            self.probes[name] = (probe, stale_after_seconds)

    def register_peer(
        self,
        dependency_name: str,
        peer_service: str,
        *,
        max_age_seconds: float = DEFAULT_STALE_SEC,
    ) -> None:
        with self.lock:
            self.peers[dependency_name] = (peer_service, max_age_seconds)

    def register_progress_watchdog(
        self,
        name: str,
        *,
        timeout_seconds: float,
        exit_code: int,
    ) -> None:
        timeout = max(30.0, float(timeout_seconds))
        with self.lock:
            self.progress_watchdogs[name] = {
                "last_progress": time.monotonic(),
                "last_publish": 0.0,
                "timeout": timeout,
                "exit_code": int(exit_code),
            }
        self.set_dependency(name, "ok", stale_after_seconds=timeout)

        def monitor() -> None:
            while not self.stop_event.wait(min(5.0, timeout / 4.0)):
                with self.lock:
                    state = dict(self.progress_watchdogs.get(name, {}))
                if not state:
                    return
                age = time.monotonic() - float(state["last_progress"])
                if age < float(state["timeout"]):
                    continue
                self.set_dependency(
                    name,
                    "unavailable",
                    data_age_seconds=age,
                    stale_after_seconds=float(state["timeout"]),
                    detail="watchdog_timeout",
                )
                exc = RuntimeError(f"ProgressWatchdogTimeout:{name}")
                logging.getLogger("perimeter-observability").critical(
                    "Progress watchdog timed out: %s", name
                )
                self.capture_exception(exc)
                self._write_heartbeat(force_ready=True)
                flush_sentry(2.0)
                os._exit(int(state["exit_code"]))

        threading.Thread(
            target=monitor,
            name=f"{self.service}-{name}-watchdog",
            daemon=True,
        ).start()

    def progress(self, name: str) -> None:
        now = time.monotonic()
        publish = True
        with self.lock:
            state = self.progress_watchdogs.get(name)
            if state is not None:
                state["last_progress"] = now
                timeout = float(state["timeout"])
                publish = now - float(state.get("last_publish", 0.0)) >= 1.0
                if publish:
                    state["last_publish"] = now
            else:
                timeout = DEFAULT_STALE_SEC
        if publish:
            self.set_dependency(name, "ok", stale_after_seconds=timeout)

    def capture_exception(self, exc: BaseException) -> None:
        try:
            import sentry_sdk

            sentry_sdk.capture_exception(exc)
        except Exception:
            pass

    def mark_fatal(self, exc: Optional[BaseException] = None) -> None:
        self.fatal = True
        if exc is not None:
            self.capture_exception(exc)
        self._write_heartbeat(force_ready=True)

    def _run_probe(self, name: str, probe: Callable[[], Optional[Mapping[str, Any]]], stale: float) -> None:
        started = time.monotonic()
        try:
            result = dict(probe() or {})
            latency = result.get("latency_ms", (time.monotonic() - started) * 1000.0)
            self.set_dependency(
                name,
                str(result.get("status", "ok")),
                latency_ms=float(latency) if latency is not None else None,
                data_age_seconds=result.get("data_age_seconds"),
                stale_after_seconds=stale,
                detail=result.get("detail"),
            )
        except Exception as exc:
            with self.lock:
                previous_status = self.dependencies.get(name, {}).get("status")
            self.set_dependency(
                name,
                "unavailable",
                latency_ms=(time.monotonic() - started) * 1000.0,
                stale_after_seconds=stale,
                detail=safe_error_name(exc),
            )
            if previous_status != "unavailable":
                self.capture_exception(exc)

    def _refresh_peers(self) -> None:
        with self.lock:
            peers = dict(self.peers)
        for dep_name, (peer_service, max_age) in peers.items():
            peer_path = self.heartbeat_path.parent / f"{peer_service}.json"
            try:
                raw = json.loads(peer_path.read_text(encoding="utf-8"))
                updated = datetime.fromisoformat(str(raw["heartbeat_at"]).replace("Z", "+00:00"))
                age = max(0.0, (utc_now() - updated.astimezone(timezone.utc)).total_seconds())
                peer_status = str(raw.get("status", "unknown")).lower()
                status = "ok" if age <= max_age and peer_status == "ok" else "unavailable"
                self.set_dependency(
                    dep_name,
                    status,
                    data_age_seconds=age,
                    stale_after_seconds=max_age,
                    detail=None if status == "ok" else "peer_not_ready",
                )
            except Exception as exc:
                self.set_dependency(
                    dep_name,
                    "unavailable",
                    stale_after_seconds=max_age,
                    detail=safe_error_name(exc),
                )

    def _dependency_snapshot(self) -> Dict[str, Dict[str, Any]]:
        now_mono = time.monotonic()
        result: Dict[str, Dict[str, Any]] = {}
        with self.lock:
            for name, raw in self.dependencies.items():
                item = {key: value for key, value in raw.items() if not key.startswith("_")}
                update_age = max(0.0, now_mono - float(raw.get("_updated_mono", now_mono)))
                item["update_age_seconds"] = round(update_age, 1)
                stale_after_raw = raw.get("stale_after_seconds", DEFAULT_STALE_SEC)
                if stale_after_raw is not None and update_age > float(stale_after_raw):
                    item["status"] = "unavailable"
                    item["detail"] = "stale"
                result[name] = item
        return result

    def snapshot(self, ready: bool = True) -> Tuple[Dict[str, Any], int]:
        if ready:
            self._refresh_peers()
        deps = self._dependency_snapshot()
        missing = [
            name
            for name in self.required_dependencies
            if deps.get(name, {}).get("status") != "ok"
        ]
        is_ready = not self.fatal and not missing
        status = "ok" if (is_ready or not ready) else "degraded"
        with self.lock:
            payload: Dict[str, Any] = {
                "status": status,
                "service": self.service,
                "version": self.version,
                "uptime_seconds": int(max(0.0, time.monotonic() - self.started_mono)),
                "started_at": iso_utc(self.started_wall),
                "heartbeat_at": iso_utc(),
                "last_success": iso_utc(self.last_success) if self.last_success else None,
            }
            if ready:
                payload["dependencies"] = deps
                payload["metrics"] = dict(self.metrics)
        return payload, 200 if (not ready or is_ready) else 503

    def _write_heartbeat(self, force_ready: bool = False) -> None:
        payload, _ = self.snapshot(ready=True)
        if force_ready:
            payload["status"] = "degraded"
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.heartbeat_path.with_suffix(self.heartbeat_path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(str(temp), str(self.heartbeat_path))

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                probes = dict(self.probes)
            for name, (probe, stale) in probes.items():
                self._run_probe(name, probe, stale)
            try:
                self._write_heartbeat()
            except Exception as exc:
                logging.getLogger("perimeter-observability").error(
                    "Cannot write health heartbeat: %s", safe_error_name(exc)
                )
                self.capture_exception(exc)
            self.stop_event.wait(self.heartbeat_sec)

    def start(self, host: str, port: int) -> None:
        reporter = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path not in {"/health", "/health/ready"}:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload, status = reporter.snapshot(ready=path.endswith("/ready"))
                raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt: str, *args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer((host, int(port)), Handler)
        self.server.daemon_threads = True
        threading.Thread(
            target=self.server.serve_forever,
            name=f"{self.service}-health",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._heartbeat_loop,
            name=f"{self.service}-heartbeat",
            daemon=True,
        ).start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


_reporter: Any = NullReporter()


def get_reporter() -> Any:
    return _reporter


def init_sentry(service: str, version: str, dsn: Optional[str] = None) -> bool:
    config = SERVICE_CONFIG.get(service, {})
    selected_dsn = dsn or os.getenv("PERIMETER_SENTRY_DSN", "") or str(config.get("dsn", ""))
    if not selected_dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration

        dedup_sec = max(10.0, float(os.getenv("PERIMETER_SENTRY_DEDUP_SEC", "300")))

        def before_send(event: Dict[str, Any], hint: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            exception_block = event.get("exception") or {}
            exception = exception_block.get("values", []) if isinstance(exception_block, dict) else []
            last_exception = exception[-1] if exception and isinstance(exception[-1], dict) else {}
            exception_type = str(last_exception.get("type", ""))
            logger_name = str(event.get("logger", ""))
            message = str(event.get("message", ""))[:120]
            key = f"{service}|{exception_type}|{logger_name}|{message}"
            now = time.monotonic()
            with _sentry_rate_lock:
                previous = _sentry_last_event.get(key, 0.0)
                if now - previous < dedup_sec:
                    return None
                _sentry_last_event[key] = now
            return event

        sentry_sdk.init(
            dsn=selected_dsn,
            release=version,
            environment=os.getenv("PERIMETER_ENVIRONMENT", "production"),
            traces_sample_rate=float(os.getenv("PERIMETER_TRACES_SAMPLE_RATE", "0.1")),
            send_default_pii=False,
            include_local_variables=False,
            attach_stacktrace=True,
            before_send=before_send,
            integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        )
        sentry_sdk.set_tag("service", service)
        sentry_sdk.set_tag("host", socket.gethostname())
        return True
    except Exception as exc:
        logging.getLogger("perimeter-observability").error(
            "Sentry initialization failed: %s", safe_error_name(exc)
        )
        return False


def init_observability(
    service: str,
    *,
    root: Path,
    start_server: bool = True,
    version: Optional[str] = None,
) -> HealthReporter:
    global _reporter
    config = SERVICE_CONFIG[service]
    release = version or os.getenv("PERIMETER_RELEASE", DEFAULT_RELEASE)
    init_sentry(service, release)
    heartbeat_dir = Path(os.getenv("PERIMETER_HEARTBEAT_DIR", str(root / "runtime" / "health")))
    reporter = HealthReporter(
        service=service,
        version=release,
        required_dependencies=tuple(config["required"]),
        heartbeat_path=heartbeat_dir / f"{service}.json",
        heartbeat_sec=float(os.getenv("PERIMETER_HEARTBEAT_SEC", str(DEFAULT_HEARTBEAT_SEC))),
    )
    _reporter = reporter
    if start_server:
        env_key = service.upper().replace(".", "_") + "_HEALTH_PORT"
        port = int(os.getenv(env_key, str(config["port"])))
        host = os.getenv("PERIMETER_HEALTH_HOST", DEFAULT_HEALTH_HOST)
        reporter.start(host, port)
    return reporter


def odbc_probe(connection_string: str, timeout: int = 5) -> Mapping[str, Any]:
    if not connection_string:
        raise RuntimeError("ConnectionStringMissing")
    import pyodbc

    started = time.monotonic()
    conn = None
    cur = None
    try:
        conn = pyodbc.connect(
            connection_string,
            autocommit=True,
            timeout=max(1, int(timeout)),
        )
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
    finally:
        # pyodbc's connection context manager commits/rolls back but does not
        # close the connection.  Explicit closes prevent a health probe every
        # ten seconds from accumulating handles during 24/7 operation.
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
    return {"status": "ok", "latency_ms": (time.monotonic() - started) * 1000.0}


def flush_sentry(timeout: float = 2.0) -> None:
    try:
        import sentry_sdk

        sentry_sdk.flush(timeout=timeout)
    except Exception:
        pass
