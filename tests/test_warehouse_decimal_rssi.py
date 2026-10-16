"""Regression tests for SQL Decimal RSSI in Warehouse retrospective evidence.

No database, reader, writer, or HA controller is contacted.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "KPP"))

from common.adaptive_windows import AdaptiveWindowModel  # noqa: E402
from kpp_aggregator_v3_warehouse_v3 import Aggregator  # noqa: E402


class _Rows:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, *args):
        self.calls.append((sql, args))
        return self

    def fetchall(self):
        return self.rows


class WarehouseDecimalRssiTests(unittest.TestCase):
    def _evidence(self, rssi):
        aggregator = Aggregator.__new__(Aggregator)
        aggregator.ADAPTIVE_WINDOWS_ENABLED = True
        aggregator.ADAPTIVE_WINDOWS_MODE = "shadow"
        aggregator.ADAPTIVE_WINDOW_HARD_MAX_SEC = 24 * 3600
        aggregator.adaptive_windows = AdaptiveWindowModel(
            default_lower_sec=0,
            default_upper_sec=3600,
            hard_max_sec=24 * 3600,
        )
        warehouse_at = datetime(2026, 10, 16, 18, 35, 0)
        rows = _Rows([(722095, warehouse_at - timedelta(seconds=90), Decimal(2), rssi)])
        tag = "E2000020590A02011510B114E20034120133040000A184C3"
        value = aggregator._retrospective_evidence(rows, tag, warehouse_at)
        self.assertEqual(len(rows.calls), 1)
        self.assertEqual(value["raw_candidates_in_page"], 1)
        self.assertEqual(value["raw_candidates"][0]["antenna"], 2)
        self.assertEqual(value["raw_candidates"][0]["id"], 722095)
        json.dumps(value, ensure_ascii=False)
        return value

    def test_odbc_decimal_rssi_serializes_into_warehouse_evidence(self):
        evidence = self._evidence(Decimal("-64.2"))
        self.assertEqual(evidence["raw_candidates"][0]["rssi"], -64.2)
        self.assertIsInstance(evidence["raw_candidates"][0]["rssi"], float)

    def test_null_rssi_remains_json_null(self):
        evidence = self._evidence(None)
        self.assertIsNone(evidence["raw_candidates"][0]["rssi"])

    def test_zero_decimal_rssi_remains_zero(self):
        evidence = self._evidence(Decimal("0.0"))
        self.assertEqual(evidence["raw_candidates"][0]["rssi"], 0.0)


if __name__ == "__main__":
    unittest.main()
