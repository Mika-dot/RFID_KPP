from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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

    def test_semantic_recovery_runs_on_reader_loop_and_preserves_cleanup(self) -> None:
        source = (ROOT / "deploy" / "monitored_rfid_recovery.py").read_text(encoding="utf-8")
        core = (ROOT / "RFID_reader_v4" / "rfid_to_sql_v4.py").read_text(encoding="utf-8")
        self.assertIn("_RecoveryController", source)
        self.assertIn("_RECOVERY.consume()", source)
        self.assertIn("RfidControlledReconnect", source)
        self.assertIn("raise SystemExit(72)", source)
        self.assertIn("finally:\n                disconnect(lib)", core)

    def test_each_inventory_start_first_stops_stale_inventory(self) -> None:
        source = (ROOT / "deploy" / "monitored_rfid_recovery.py").read_text(encoding="utf-8")
        block = source.split("    def UHFInventory(self, *args):", 1)[1].split("    def UHF_GetReceived_EX", 1)[0]
        self.assertIn("super().UHFStopGet()", block)
        self.assertIn("super().UHFInventory(*args)", block)
        self.assertLess(block.index("super().UHFStopGet()"), block.index("super().UHFInventory(*args)"))

    def test_periodic_process_recycle_is_graceful_not_hard_exit(self) -> None:
        source = (ROOT / "deploy" / "monitored_rfid_recovery.py").read_text(encoding="utf-8")
        logic = source.split("class _BusinessFlowWatchdog", 1)[1]
        self.assertIn("RFID_BUSINESS_PROCESS_RECYCLE_EVERY", logic)
        self.assertIn("raise SystemExit(72)", logic)
        self.assertNotIn("os._exit(72)", logic)


if __name__ == "__main__":
    unittest.main()
