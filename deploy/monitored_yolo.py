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
    writer_stale_sec = max(
        30.0,
        float(app.os.getenv("RFID_VIDEO_DELIVERY_WRITER_WATCHDOG_SEC", "90")),
    )

    for dependency in ("local_spool", "delivery_writer"):
        if dependency not in reporter.required_dependencies:
            reporter.required_dependencies = tuple(reporter.required_dependencies) + (dependency,)

    reporter.register_progress_watchdog(
        "pipeline",
        timeout_seconds=float(app.os.getenv("RFID_YOLO_WATCHDOG_SEC", "120")),
        exit_code=71,
    )
    reporter.set_dependency(
        "pipeline",
        "unknown",
        stale_after_seconds=float(app.os.getenv("RFID_YOLO_WATCHDOG_SEC", "120")),
    )
    reporter.set_dependency("delivery_writer", "unknown", stale_after_seconds=writer_stale_sec)
    reporter.set_dependency("local_spool", "unknown", stale_after_seconds=None)

    rtsp_block_sec = max(
        15.0,
        float(app.os.getenv("RFID_RTSP_READ_WATCHDOG_SEC", "45")),
    )

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

    # Video spool is the durability boundary. Core logic deliberately retains
    # transitions in RAM and retries when enqueue fails. Therefore writer/spool
    # faults must make readiness red but must NOT kill the process and discard
    # those undurable transitions.
    original_spool_init = app.DurableEventSpool.__init__
    original_spool_enqueue = app.DurableEventSpool.enqueue
    original_next_pending = app.DurableEventSpool.next_pending
    original_writer_run = app.DBWriter.run

    def monitored_spool_init(spool, path):
        original_spool_init(spool, path)
        reporter.touch_dependency("local_spool", stale_after_seconds=None)

    def monitored_spool_enqueue(spool, event):
        result = original_spool_enqueue(spool, event)
        if result:
            reporter.touch_dependency("local_spool", stale_after_seconds=None)
        else:
            reporter.set_dependency(
                "local_spool",
                "unavailable",
                stale_after_seconds=None,
                detail="durable_enqueue_failed",
            )
        return result

    def monitored_next_pending(spool):
        try:
            result = original_next_pending(spool)
            reporter.set_dependency(
                "delivery_writer",
                "ok",
                stale_after_seconds=writer_stale_sec,
            )
            return result
        except Exception as exc:
            reporter.set_dependency(
                "delivery_writer",
                "unavailable",
                stale_after_seconds=writer_stale_sec,
                detail=safe_error_name(exc),
            )
            reporter.set_dependency(
                "local_spool",
                "unavailable",
                stale_after_seconds=None,
                detail=safe_error_name(exc),
            )
            raise

    def resilient_writer_run(writer):
        while writer.running:
            try:
                original_writer_run(writer)
                return
            except Exception as exc:
                reporter.set_dependency(
                    "delivery_writer",
                    "unavailable",
                    stale_after_seconds=writer_stale_sec,
                    detail=safe_error_name(exc),
                )
                reporter.capture_exception(exc)
                time.sleep(1.0)

    app.DurableEventSpool.__init__ = monitored_spool_init
    app.DurableEventSpool.enqueue = monitored_spool_enqueue
    app.DurableEventSpool.next_pending = monitored_next_pending
    app.DBWriter.run = resilient_writer_run

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
