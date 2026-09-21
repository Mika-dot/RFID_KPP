from __future__ import annotations

import unittest

from deploy import monitored_rfid_recovery_v2 as recovery_v2


class _Reporter:
    def __init__(self) -> None:
        self.events = []

    def progress(self, name, *args, **kwargs):
        self.events.append(("progress", name))

    def touch_dependency(self, name, *args, **kwargs):
        self.events.append(("touch", name))

    def set_dependency(self, name, status, *args, **kwargs):
        self.events.append(("set", name, status))

    def set_metric(self, *args, **kwargs):
        pass

    def capture_exception(self, *args, **kwargs):
        pass

    def mark_fatal(self, *args, **kwargs):
        pass


class _Watchdog:
    def call(self, name, func, *args):
        return func(*args)


class _Lib:
    def UHF_GetReceived_EX(self, *args):
        return 0


class RfidTransportHealthTests(unittest.TestCase):
    def test_successful_receive_refreshes_rfid_tcp(self) -> None:
        reporter = _Reporter()
        proxy = recovery_v2._LibraryProxy(_Lib(), reporter, _Watchdog(), {-1})
        self.assertEqual(proxy.UHF_GetReceived_EX(None, None), 0)
        self.assertIn(("touch", "rfid_tcp"), reporter.events)

    def test_wrapper_uses_transport_health_adapter(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        wrapper = (root / "deploy" / "RUN_RFID_READER_V3.cmd").read_text(encoding="ascii")
        self.assertIn("monitored_rfid_recovery_v2.py", wrapper)


if __name__ == "__main__":
    unittest.main()
