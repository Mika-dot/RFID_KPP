#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telemetry adapter for the current RusGuard sync loop."""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "DB_RusGard"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from common.observability import get_reporter, safe_error_name  # noqa: E402


def _load_app():
    path = SERVICE_DIR / "db_sync_v2.py"
    spec = importlib.util.spec_from_file_location("perimeter_current_rusguard", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("RusGuardModuleLoadFailed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    app = _load_app()
    reporter = get_reporter()
    reporter.register_progress_watchdog(
        "sync_loop",
        timeout_seconds=float(app.os.getenv("RUSGUARD_WATCHDOG_SEC", "180")),
        exit_code=72,
    )

    instance_lock = app.SingleInstanceLock(app.LOCK_PATH)
    last_heartbeat = 0.0
    total_rows = 0
    last_cursor = 0
    app.log.info("RusGuard Sync v3.4 + observability adapter started")

    while True:
        try:
            count, last_cursor = app.sync_page()
            reporter.progress("sync_loop")
            reporter.touch_dependency("source_database")
            reporter.touch_dependency("destination_database")
            reporter.mark_success()
            total_rows += count
            now = time.monotonic()
            if count >= app.BATCH_SIZE:
                continue
            if now - last_heartbeat >= app.HEARTBEAT_SEC:
                reporter.set_metric("cursor", last_cursor)
                reporter.set_metric("new_rows_last_poll", count)
                reporter.set_metric("total_rows_this_run", total_rows)
                last_heartbeat = now
            time.sleep(app.POLL_SEC)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            # Keep the watchdog alive while the loop is actively retrying, but
            # leave readiness degraded until a successful iteration occurs.
            reporter.progress("sync_loop")
            reporter.set_dependency("sync_loop", "unavailable", detail=safe_error_name(exc))
            app.log.exception("RusGuard sync failed; cursor not changed")
            time.sleep(max(5.0, app.POLL_SEC))


if __name__ == "__main__":
    raise SystemExit(main())
