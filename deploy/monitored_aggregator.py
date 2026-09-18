#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telemetry adapter for the current 3.4.5 Warehouse aggregator."""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "KPP"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from common.observability import flush_sentry, get_reporter, safe_error_name  # noqa: E402


def _load_app():
    path = SERVICE_DIR / "kpp_aggregator_v3_warehouse_v3.py"
    spec = importlib.util.spec_from_file_location("perimeter_current_aggregator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("AggregatorModuleLoadFailed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    app = _load_app()
    reporter = get_reporter()
    max_consecutive_failures = max(
        1,
        int(os.getenv("KPP_CONSECUTIVE_FAILURE_RESTART", "5")),
    )
    consecutive_failures = 0

    if not args.once:
        reporter.register_progress_watchdog(
            "pipeline",
            timeout_seconds=float(os.getenv("KPP_WATCHDOG_SEC", "180")),
            exit_code=73,
        )

    aggregator = app.Aggregator()
    aggregator.bootstrap()
    aggregator.reconcile_warehouse(force=True)
    reporter.progress("pipeline")
    reporter.touch_dependency("database")
    reporter.mark_success()

    while True:
        try:
            count = aggregator.process_once()
            aggregator.recheck_pending()
            wh_count = aggregator.reconcile_warehouse()
            consecutive_failures = 0
            reporter.progress("pipeline")
            reporter.touch_dependency("database")
            reporter.mark_success()
            reporter.set_metric("last_rfid_id", getattr(aggregator, "last_rfid_id", 0))
            reporter.set_metric("consecutive_failures", 0)
            if args.once:
                return 0
            if count >= app.Config.RFID_BATCH_SIZE or wh_count >= aggregator.WAREHOUSE_BATCH_SIZE:
                continue
            time.sleep(app.Config.POLL_SEC)
        except KeyboardInterrupt:
            app.log.warning(
                "Остановка пользователем. Активные сессии уже durable, искусственно не закрываются."
            )
            return 0
        except Exception as exc:
            # IMPORTANT: do NOT call reporter.progress() here.  The old adapter
            # refreshed the watchdog on every failed iteration, so an endlessly
            # failing aggregator could remain alive forever and never trigger
            # the existing CMD restart supervisor.
            consecutive_failures += 1
            reporter.set_metric("consecutive_failures", consecutive_failures)
            reporter.set_dependency("pipeline", "unavailable", detail=safe_error_name(exc))
            reporter.set_dependency("database", "unavailable", detail=safe_error_name(exc))
            reporter.capture_exception(exc)
            app.log.exception(
                "Aggregator iteration failed (%s/%s)",
                consecutive_failures,
                max_consecutive_failures,
            )
            if args.once:
                raise
            if consecutive_failures >= max_consecutive_failures:
                reporter.capture_exception(
                    RuntimeError(
                        f"AggregatorConsecutiveFailures:{consecutive_failures}"
                    )
                )
                flush_sentry(2.0)
                # RUN_AGGREGATOR_V3.cmd sees the non-zero exit and restarts the
                # process after 5 seconds. Durable cursors/sessions remain in SQL.
                os._exit(73)
            time.sleep(max(2.0, app.Config.POLL_SEC))


if __name__ == "__main__":
    raise SystemExit(main())
