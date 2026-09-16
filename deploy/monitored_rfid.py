#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telemetry adapter for the current RFID reader without changing its business logic."""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "RFID_reader_v4"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from common.observability import flush_sentry, get_reporter, safe_error_name  # noqa: E402


def _load_app():
    path = SERVICE_DIR / "rfid_to_sql_v4.py"
    spec = importlib.util.spec_from_file_location("perimeter_current_rfid", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("RfidModuleLoadFailed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _SdkCallWatchdog:
    def __init__(self, reporter, timeout_sec: float) -> None:
        self.reporter = reporter
        self.timeout_sec = max(5.0, float(timeout_sec))
        self.lock = threading.Lock()
        self.active_name = ""
        self.active_since = 0.0
        threading.Thread(target=self._loop, name="rfid-sdk-monitor", daemon=True).start()

    def call(self, name: str, func, *args):
        with self.lock:
            self.active_name = name
            self.active_since = time.monotonic()
        try:
            return func(*args)
        finally:
            with self.lock:
                self.active_name = ""
                self.active_since = 0.0

    def _loop(self) -> None:
        while True:
            time.sleep(0.5)
            with self.lock:
                name = self.active_name
                age = time.monotonic() - self.active_since if name else 0.0
            if not name or age < self.timeout_sec:
                continue
            self.reporter.set_dependency(
                "rfid_reader",
                "unavailable",
                data_age_seconds=age,
                detail="sdk_call_timeout",
            )
            exc = RuntimeError(f"RfidSdkCallTimeout:{name}")
            self.reporter.capture_exception(exc)
            flush_sentry(2.0)
            os._exit(70)


class _LibraryProxy:
    def __init__(self, lib, reporter, watchdog, no_data_codes: set[int]) -> None:
        self._lib = lib
        self._reporter = reporter
        self._watchdog = watchdog
        self._no_data_codes = no_data_codes

    def __getattr__(self, name):
        return getattr(self._lib, name)

    def TCPConnect(self, *args):
        try:
            rc = self._watchdog.call("TCPConnect", self._lib.TCPConnect, *args)
        except BaseException as exc:
            self._reporter.set_dependency("rfid_reader", "unavailable", detail=safe_error_name(exc))
            raise
        if int(rc) == 0:
            self._reporter.touch_dependency("rfid_reader")
        else:
            self._reporter.set_dependency("rfid_reader", "unavailable", detail=f"connect_code_{int(rc)}")
        return rc

    def UHFInventory(self, *args):
        return self._watchdog.call("UHFInventory", self._lib.UHFInventory, *args)

    def UHF_GetReceived_EX(self, *args):
        try:
            rc = self._watchdog.call("UHF_GetReceived_EX", self._lib.UHF_GetReceived_EX, *args)
        except BaseException as exc:
            self._reporter.set_dependency("rfid_reader", "unavailable", detail=safe_error_name(exc))
            raise
        code = int(rc)
        if code == 0 or code in self._no_data_codes:
            self._reporter.touch_dependency("rfid_reader")
        else:
            self._reporter.set_dependency("rfid_reader", "unavailable", detail=f"receive_code_{code}")
        return rc

    def UHFStopGet(self, *args):
        return self._watchdog.call("UHFStopGet", self._lib.UHFStopGet, *args)

    def TCPDisconnect(self, *args):
        try:
            return self._watchdog.call("TCPDisconnect", self._lib.TCPDisconnect, *args)
        finally:
            self._reporter.set_dependency("rfid_reader", "unavailable", detail="disconnected")


def main() -> int:
    app = _load_app()
    reporter = get_reporter()
    watchdog = _SdkCallWatchdog(
        reporter,
        float(os.getenv("RFID_SDK_CALL_TIMEOUT_SEC", "30")),
    )
    no_data_codes: set[int] = set()
    for raw in os.getenv("RFID_SDK_NO_DATA_CODES", "-1").split(","):
        try:
            no_data_codes.add(int(raw.strip()))
        except ValueError:
            pass

    original_load_library = app.load_library

    def monitored_load_library():
        lib = original_load_library()
        return _LibraryProxy(lib, reporter, watchdog, no_data_codes)

    app.load_library = monitored_load_library
    return int(app.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
