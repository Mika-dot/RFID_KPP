from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from deploy import monitored_rfid_recovery as recovery


ROOT = Path(__file__).resolve().parents[1]


class _Reporter:
    def __init__(self) -> None:
        self.events = []
        self.probes = {"rfid_tcp": (object(), 45.0), "database": (object(), 45.0)}
        import threading
        self.lock = threading.RLock()

    def progress(self, name, *args, **kwargs):
        self.events.append(("progress", name))

    def touch_dependency(self, name, *args, **kwargs):
        self.events.append(("touch", name))

    def set_dependency(self, name, status, *args, **kwargs):
        self.events.append(("set", name, status, kwargs.get("detail")))

    def set_metric(self, *args, **kwargs):
        pass

    def capture_exception(self, *args, **kwargs):
        pass

    def mark_fatal(self, *args, **kwargs):
        pass


class _Watchdog:
    def call(self, name, func, *args):
        return func(*args)


class RfidContinuousRecoveryTests(unittest.TestCase):
    def test_production_wrapper_uses_continuous_recovery_adapter(self) -> None:
        wrapper = (ROOT / "deploy" / "RUN_RFID_READER_V3.cmd").read_text(encoding="ascii")
        self.assertIn("monitored_rfid_recovery.py", wrapper)
        self.assertIn("PERIMETER_RELEASE=3.4.7-resilience-audit", wrapper)
        self.assertNotIn('--script "%ROOT%\\deploy\\monitored_rfid.py"', wrapper)

    def test_recovery_never_has_terminal_three_attempt_exhaustion(self) -> None:
        source = (ROOT / "deploy" / "monitored_rfid_recovery.py").read_text(encoding="utf-8")
        logic = source.split("class _BusinessFlowWatchdog", 1)[1]
        self.assertIn("RFID_BUSINESS_RECOVERY_BACKOFF_SEC", logic)
        self.assertIn('"self_heal_exhausted", False', logic)
        self.assertIn('"self_heal_continues"', logic)
        self.assertNotIn("RfidBusinessFlowSelfHealExhausted", logic)
        self.assertNotIn("os._exit(72)", logic)

    def test_existing_latched_fault_is_recovered_immediately_after_upgrade(self) -> None:
        source = (ROOT / "deploy" / "monitored_rfid_recovery.py").read_text(encoding="utf-8")
        self.assertIn(
            "self.state.fault_latched and self.state.restart_attempts >= self.max_restarts",
            source,
        )
        self.assertIn("due = True", source)

    def test_semantic_recovery_runs_on_reader_loop_and_preserves_cleanup(self) -> None:
        source = (ROOT / "deploy" / "monitored_rfid_recovery.py").read_text(encoding="utf-8")
        self.assertIn("_RecoveryController", source)
        self.assertIn("_RECOVERY.consume()", source)
        self.assertIn("RfidControlledReconnect", source)
        self.assertIn("raise SystemExit(72)", source)
        self.assertIn("app.disconnect(lib)", source)

    def test_inventory_start_does_not_issue_speculative_stop(self) -> None:
        calls = []

        class Lib:
            def UHFInventory(self):
                calls.append("inventory")
                return 0

            def UHFStopGet(self):
                calls.append("stop")
                return 0

        reporter = _Reporter()
        proxy = recovery._LibraryProxy(Lib(), reporter, _Watchdog(), {-1})
        self.assertEqual(proxy.UHFInventory(), 0)
        self.assertEqual(calls, ["inventory"])

    def test_inventory_failure_is_contained_in_process_and_disconnects(self) -> None:
        class Lib:
            def UHFInventory(self):
                raise RuntimeError("inventory failed")

        class Config:
            RECONNECT_SEC = 0.0

        class App:
            Config = Config

            def __init__(self):
                self.disconnect_calls = 0

            def disconnect(self, lib):
                self.disconnect_calls += 1

        app = App()
        with mock.patch.object(recovery.time, "sleep", return_value=None):
            result = recovery._start_inventory_or_recover(app, Lib())
        self.assertFalse(result)
        self.assertEqual(app.disconnect_calls, 1)

    def test_system_exit_from_inventory_is_not_swallowed(self) -> None:
        class Lib:
            def UHFInventory(self):
                raise SystemExit(72)

        class Config:
            RECONNECT_SEC = 0.0

        class App:
            Config = Config

            @staticmethod
            def disconnect(lib):
                raise AssertionError("must be handled by outer finally")

        with self.assertRaises(SystemExit) as ctx:
            recovery._start_inventory_or_recover(App(), Lib())
        self.assertEqual(ctx.exception.code, 72)

    def test_rfid_tcp_health_is_owned_by_sdk_connect_not_second_socket(self) -> None:
        calls = []

        class Lib:
            def TCPConnect(self, *args):
                calls.append("connect")
                return 0

        reporter = _Reporter()
        proxy = recovery._LibraryProxy(Lib(), reporter, _Watchdog(), {-1})
        self.assertEqual(proxy.TCPConnect(b"127.0.0.1", 8888), 0)
        self.assertIn(("touch", "rfid_tcp"), reporter.events)

    def test_competing_rfid_tcp_probe_is_removed(self) -> None:
        reporter = _Reporter()
        recovery._disable_competing_tcp_probe(reporter)
        self.assertNotIn("rfid_tcp", reporter.probes)
        self.assertIn("database", reporter.probes)
        self.assertTrue(
            any(event[:3] == ("set", "rfid_tcp", "unknown") for event in reporter.events)
        )

    def test_periodic_process_recycle_is_graceful_not_hard_exit(self) -> None:
        source = (ROOT / "deploy" / "monitored_rfid_recovery.py").read_text(encoding="utf-8")
        logic = source.split("class _BusinessFlowWatchdog", 1)[1]
        self.assertIn("RFID_BUSINESS_PROCESS_RECYCLE_EVERY", logic)
        self.assertIn("raise SystemExit(72)", logic)
        self.assertNotIn("os._exit(72)", logic)


if __name__ == "__main__":
    unittest.main()
