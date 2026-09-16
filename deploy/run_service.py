#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run one current Perimeter service behind the existing observability contract.

The production service script is executed unchanged.  This wrapper restores:
- Sentry initialization and crash reporting;
- local /health and /health/ready endpoints on ports 18101..18105;
- runtime/health heartbeat JSON used by peer checks and Zabbix.
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import runpy
import socket
import sys
import time
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlparse


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


def tcp_probe(host: str, port: int, timeout: float = 3.0) -> Mapping[str, object]:
    if not host:
        raise RuntimeError("HostMissing")
    started = time.monotonic()
    with socket.create_connection((host, int(port)), timeout=max(0.5, float(timeout))):
        pass
    return {
        "status": "ok",
        "latency_ms": (time.monotonic() - started) * 1000.0,
    }


def rtsp_tcp_probe(url: str) -> Mapping[str, object]:
    if not url:
        raise RuntimeError("RtspUrlMissing")
    parsed = urlparse(url)
    if not parsed.hostname:
        raise RuntimeError("RtspHostMissing")
    return tcp_probe(parsed.hostname, parsed.port or 554, 3.0)


def file_probe(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return {"status": "ok", "latency_ms": 0.0}


def _register_dependency_checks(service: str, reporter, script: Path) -> None:
    if service == "Perimeter.RfidReader":
        reporter.register_probe(
            "database",
            lambda: odbc_probe(os.getenv("RFID_DB_CONNECTION", ""), 5),
        )
        reporter.register_probe(
            "rfid_reader",
            lambda: tcp_probe(
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
        # The actual sync code remains the current production script.  If it
        # exits or throws, the runner is no longer healthy and Sentry records it.
        reporter.touch_dependency("sync_loop", stale_after_seconds=None)
        return

    if service == "Perimeter.Yolo":
        reporter.register_probe(
            "database",
            lambda: odbc_probe(os.getenv("RFID_DB_CONNECTION", ""), 5),
        )

        raw_model = os.getenv(
            "RFID_MODEL_PATH",
            "runs/detect/rfid_forklift_reel2/weights/best.pt",
        )
        model_path = Path(raw_model)
        if not model_path.is_absolute():
            model_path = (script.parent / model_path).resolve()
        reporter.register_probe("model", lambda: file_probe(model_path))

        for camera_id in (0, 1):
            reporter.register_probe(
                f"camera_{camera_id}",
                lambda cid=camera_id: rtsp_tcp_probe(os.getenv(f"RFID_RTSP_{cid}", "")),
            )
        reporter.touch_dependency("pipeline", stale_after_seconds=None)
        return

    if service == "Perimeter.Aggregator":
        reporter.register_probe(
            "database",
            lambda: odbc_probe(os.getenv("KPP_CONN_STR", ""), 5),
        )
        reporter.touch_dependency("pipeline", stale_after_seconds=None)
        reporter.register_peer("rfid_reader", "Perimeter.RfidReader")
        reporter.register_peer("yolo", "Perimeter.Yolo")
        reporter.register_peer("rusguard", "Perimeter.RusGuardSync")
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
        try:
            reporter.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
