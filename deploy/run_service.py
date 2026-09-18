#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run one current Perimeter service behind the restored observability contract."""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import runpy
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability import (  # noqa: E402
    DEFAULT_HEALTH_HOST,
    SERVICE_CONFIG,
    flush_sentry,
    init_observability,
    odbc_probe,
    safe_error_name,
)

log = logging.getLogger("perimeter-service-runner")


def _disable_windows_crash_dialogs() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass


def _required_env(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise RuntimeError(f"EnvironmentVariableMissing:{name}")
    return value


def _sql_connection(prefix: str) -> str:
    return (
        f"DRIVER={{{os.getenv(prefix + '_DRIVER', 'ODBC Driver 18 for SQL Server')}}};"
        f"SERVER={_required_env(prefix + '_SERVER')};"
        f"DATABASE={_required_env(prefix + '_DATABASE')};"
        f"UID={_required_env(prefix + '_USERNAME')};"
        f"PWD={_required_env(prefix + '_PASSWORD')};"
        "Encrypt=yes;TrustServerCertificate=yes;"
    )


def _file_probe(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return {"status": "ok", "latency_ms": 0.0}


def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> Mapping[str, object]:
    if not host:
        raise RuntimeError("HostMissing")
    started = time.monotonic()
    with socket.create_connection((host, int(port)), timeout=max(0.5, float(timeout))):
        pass
    return {
        "status": "ok",
        "latency_ms": (time.monotonic() - started) * 1000.0,
    }


def _require_dependency(reporter, name: str) -> None:
    if name in reporter.required_dependencies:
        return
    reporter.required_dependencies = tuple(reporter.required_dependencies) + (name,)
    reporter.set_dependency(name, "unknown")


def _install_robust_heartbeat_writer(reporter) -> None:
    """Avoid Windows temp-file collisions/PermissionError in heartbeat writes."""

    def robust_write(force_ready: bool = False) -> None:
        payload, _ = reporter.snapshot(ready=True)
        if force_ready:
            payload["status"] = "degraded"
        path = reporter.heartbeat_path
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        last_error: Optional[BaseException] = None

        for attempt in range(5):
            temp = path.with_name(
                f"{path.name}.{os.getpid()}.{threading.get_ident()}.{attempt}.tmp"
            )
            try:
                temp.write_text(raw, encoding="utf-8")
                os.replace(str(temp), str(path))
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
        raise RuntimeError("HeartbeatWriteFailed")

    reporter._write_heartbeat = robust_write


def _register_dependency_checks(service: str, reporter, target: Path) -> None:
    if service == "Perimeter.RfidReader":
        reporter.register_probe(
            "database",
            lambda: odbc_probe(os.getenv("RFID_DB_CONNECTION", ""), 5),
        )
        # TCP/SDK health alone is insufficient. A reader may keep answering
        # while the actual RFID business data-plane is stalled. The monitored
        # adapter owns this semantic dependency using cross-source evidence.
        _require_dependency(reporter, "business_flow")
        # SDK state comes from monitored_rfid.py. Keep a separate physical TCP
        # dependency so a dead reader endpoint cannot be masked by the process.
        _require_dependency(reporter, "rfid_tcp")
        reporter.register_probe(
            "rfid_tcp",
            lambda: _tcp_probe(
                os.getenv("RFID_READER_IP", ""),
                int(os.getenv("RFID_READER_PORT", "8888")),
                3.0,
            ),
        )
        return

    if service == "Perimeter.RusGuardSync":
        reporter.register_probe(
            "source_database",
            lambda: odbc_probe(_sql_connection("SRC"), 5),
        )
        reporter.register_probe(
            "destination_database",
            lambda: odbc_probe(_sql_connection("DST"), 5),
        )
        # sync_loop is updated by monitored_rusguard.py.
        return

    if service == "Perimeter.Yolo":
        reporter.register_probe(
            "database",
            lambda: odbc_probe(os.getenv("RFID_DB_CONNECTION", ""), 5),
        )
        raw_model = os.getenv(
            "RFID_MODEL_PATH",
            str(ROOT / "RTSP" / "runs" / "detect" / "rfid_forklift_reel2" / "weights" / "best.pt"),
        )
        model_path = Path(raw_model)
        if not model_path.is_absolute():
            model_path = (ROOT / "RTSP" / model_path).resolve()
        reporter.register_probe("model", lambda: _file_probe(model_path))
        # camera_0/camera_1/pipeline are driven from actual frames/main loop by
        # monitored_yolo.py rather than by a weak RTSP TCP-port probe.
        return

    if service == "Perimeter.Aggregator":
        reporter.register_probe(
            "database",
            lambda: odbc_probe(os.getenv("KPP_CONN_STR", ""), 5),
        )
        reporter.register_peer("rfid_reader", "Perimeter.RfidReader")
        reporter.register_peer("yolo", "Perimeter.Yolo")
        reporter.register_peer("rusguard", "Perimeter.RusGuardSync")
        # pipeline is driven by monitored_aggregator.py.
        return

    if service == "Perimeter.WebDashboard":
        reporter.register_probe(
            "database",
            lambda: odbc_probe(
                os.getenv(
                    "KPP_WEB_DB_CONNECTION",
                    os.getenv("RFID_DB_CONNECTION", ""),
                ),
                5,
            ),
        )
        reporter.register_peer("aggregator", "Perimeter.Aggregator")
        _require_dependency(reporter, "web_port")
        reporter.register_probe(
            "web_port",
            lambda: _tcp_probe(
                "127.0.0.1",
                int(os.getenv("KPP_WEB_PORT", "5050")),
                3.0,
            ),
        )
        return


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True, choices=sorted(SERVICE_CONFIG))
    parser.add_argument("--script", required=True)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    script = Path(args.script).resolve()
    if not script.is_file():
        print(f"[FATAL] Service script is missing: {script}", file=sys.stderr)
        return 2

    _disable_windows_crash_dialogs()
    reporter = init_observability(
        args.service,
        root=ROOT,
        start_server=False,
    )
    _install_robust_heartbeat_writer(reporter)
    _register_dependency_checks(args.service, reporter, script)

    cfg = SERVICE_CONFIG[args.service]
    env_key = args.service.upper().replace(".", "_") + "_HEALTH_PORT"
    health_port = int(os.getenv(env_key, str(cfg["port"])))
    health_host = os.getenv("PERIMETER_HEALTH_HOST", DEFAULT_HEALTH_HOST)
    reporter.start(health_host, health_port)

    log.info(
        "Starting %s release=%s health=%s:%s script=%s",
        args.service,
        reporter.version,
        health_host,
        health_port,
        script.name,
    )

    runner_argv = sys.argv
    target_dir = str(script.parent)
    inserted_target_dir = target_dir not in sys.path
    if inserted_target_dir:
        # runpy.run_path does not provide normal script-directory import
        # semantics. Current Warehouse/Web wrappers use sibling imports, so the
        # target directory must be visible exactly as with `python script.py`.
        sys.path.insert(0, target_dir)
    sys.argv = [str(script)]

    try:
        runpy.run_path(str(script), run_name="__main__")
        return 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if code:
            reporter.mark_fatal(RuntimeError(f"ServiceExitCode{code}"))
            flush_sentry(3.0)
        return int(code)
    except KeyboardInterrupt:
        return 0
    except BaseException as exc:
        log.critical(
            "Unhandled %s failure: %s",
            args.service,
            safe_error_name(exc),
            exc_info=True,
        )
        reporter.mark_fatal(exc)
        flush_sentry(5.0)
        return 1
    finally:
        sys.argv = runner_argv
        if inserted_target_dir:
            try:
                sys.path.remove(target_dir)
            except ValueError:
                pass
        try:
            reporter.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
