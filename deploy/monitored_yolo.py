#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telemetry/self-heal adapter for the current RTSP/YOLO pipeline."""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "RTSP"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from common.observability import flush_sentry, get_reporter, safe_error_name  # noqa: E402


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
    rtsp_block_sec = max(
        15.0,
        float(app.os.getenv("RFID_RTSP_READ_WATCHDOG_SEC", "45")),
    )

    # OpenCV/FFmpeg may block forever inside VideoCapture.read(). The main YOLO
    # loop continues polling read_new(), so the ordinary pipeline watchdog alone
    # cannot detect this failure. Replace the capture loop with equivalent logic
    # that exposes a monotonic heartbeat around each potentially blocking call.
    original_init = app.RTSPStream.__init__

    def monitored_init(stream, url, camera_id):
        original_init(stream, url, camera_id)
        stream.capture_progress_mono = time.monotonic()

    def monitored_capture_loop(stream):
        while stream.running:
            stream.capture_progress_mono = time.monotonic()
            try:
                if stream.cap:
                    stream.cap.release()
                backend = (
                    app.cv2.CAP_FFMPEG
                    if app.Config.RTSP_BACKEND == "FFMPEG"
                    else app.cv2.CAP_ANY
                )
                stream.capture_progress_mono = time.monotonic()
                stream.cap = app.cv2.VideoCapture(stream.url, backend)
                stream.capture_progress_mono = time.monotonic()
                if app.Config.SET_CAPTURE_BUFFER:
                    try:
                        stream.cap.set(app.cv2.CAP_PROP_BUFFERSIZE, 1)
                    except Exception:
                        app.log.debug(
                            "Camera %s does not support CAP_PROP_BUFFERSIZE",
                            stream.camera_id,
                        )
                if not stream.cap.isOpened():
                    raise ConnectionError("поток не открыт")
                app.log.info("Камера %s подключена", stream.camera_id)
                while stream.running:
                    # If read() itself blocks, this timestamp stops moving and
                    # monitored_read_new below forces a full process rebuild.
                    stream.capture_progress_mono = time.monotonic()
                    ok, frame = stream.cap.read()
                    stream.capture_progress_mono = time.monotonic()
                    if not ok:
                        raise ConnectionError("кадр не получен")
                    packet = app.FramePacket(
                        frame.copy(),
                        app.datetime.now(),
                        time.monotonic(),
                        stream.sequence + 1,
                    )
                    with stream.lock:
                        stream.sequence = packet.sequence
                        stream.latest = packet
            except Exception as exc:
                stream.capture_progress_mono = time.monotonic()
                app.log.warning(
                    "Камера %s: %s; reconnect",
                    stream.camera_id,
                    exc,
                )
                time.sleep(app.Config.RECONNECT_SEC)
                stream.capture_progress_mono = time.monotonic()

    app.RTSPStream.__init__ = monitored_init
    app.RTSPStream._loop = monitored_capture_loop

    original_read_new = app.RTSPStream.read_new

    def monitored_read_new(stream, after_sequence):
        # Called from the main inference loop. If model.predict or the pipeline
        # blocks, these progress updates stop and the pipeline watchdog restarts.
        reporter.progress("pipeline")

        capture_age = max(
            0.0,
            time.monotonic()
            - float(getattr(stream, "capture_progress_mono", time.monotonic())),
        )
        if capture_age >= rtsp_block_sec:
            dep = f"camera_{stream.camera_id}"
            exc = RuntimeError(
                f"RtspCaptureThreadBlocked:camera={stream.camera_id}:age={capture_age:.1f}s"
            )
            reporter.set_dependency(
                dep,
                "unavailable",
                data_age_seconds=capture_age,
                detail="capture_thread_blocked",
            )
            reporter.mark_fatal(exc)
            flush_sentry(2.0)
            # RUN_RTSP_V3.cmd restarts the entire OpenCV/FFmpeg process. A mere
            # reconnect inside the stuck thread is impossible while read() has
            # not returned.
            os._exit(74)

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
