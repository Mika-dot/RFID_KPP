#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telemetry adapter for the current RTSP/YOLO pipeline."""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "RTSP"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from common.observability import get_reporter, safe_error_name  # noqa: E402


def _load_app():
    path = SERVICE_DIR / "RTSP_yolo_DB_v3.py"
    spec = importlib.util.spec_from_file_location("perimeter_current_yolo", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("YoloModuleLoadFailed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    app = _load_app()
    reporter = get_reporter()
    reporter.register_progress_watchdog(
        "pipeline",
        timeout_seconds=float(app.os.getenv("RFID_YOLO_WATCHDOG_SEC", "120")),
        exit_code=71,
    )

    original_read_new = app.RTSPStream.read_new

    def monitored_read_new(stream, after_sequence):
        # Called from the main inference loop. If model.predict or the pipeline
        # blocks, these progress updates stop and the watchdog restarts service.
        reporter.progress("pipeline")
        packet = original_read_new(stream, after_sequence)
        dep = f"camera_{stream.camera_id}"
        if packet is not None:
            reporter.touch_dependency(dep, data_age_seconds=0.0)
            return packet

        with stream.lock:
            latest = stream.latest
        if latest is None:
            reporter.set_dependency(dep, "unavailable", detail="no_frames")
            return None

        age = max(0.0, time.monotonic() - latest.captured_mono)
        stale_limit = max(5.0, float(app.Config.MAX_FRAME_AGE_SEC) * 3.0)
        if age <= stale_limit:
            reporter.touch_dependency(dep, data_age_seconds=age)
        else:
            reporter.set_dependency(
                dep,
                "unavailable",
                data_age_seconds=age,
                detail="stale_frames",
            )
        return None

    app.RTSPStream.read_new = monitored_read_new

    original_insert = app.DBWriter.insert

    def monitored_insert(payload, image):
        try:
            result = original_insert(payload, image)
            reporter.touch_dependency("database")
            return result
        except Exception as exc:
            reporter.set_dependency("database", "unavailable", detail=safe_error_name(exc))
            reporter.capture_exception(exc)
            raise

    app.DBWriter.insert = staticmethod(monitored_insert)

    return int(app.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
