from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# В тестовой среде нет SQL Server ODBC. Для чистых методов достаточно заглушки.
sys.modules.setdefault("pyodbc", types.SimpleNamespace(Connection=object, Cursor=object, connect=None))
MODULE_PATH = Path(__file__).resolve().parents[1] / "KPP" / "kpp_aggregator_v3.py"
spec = importlib.util.spec_from_file_location("kpp_aggregator_v3_test", MODULE_PATH)
aggmod = importlib.util.module_from_spec(spec)
assert spec.loader
sys.modules[spec.name] = aggmod
spec.loader.exec_module(aggmod)

from common.kpp_core_v3 import RfidRead, StrictSessionizer  # noqa: E402


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args, **_kwargs):
        return self

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)


class AggregatorReliabilityTests(unittest.TestCase):
    def make_aggregator(self):
        obj = aggmod.Aggregator.__new__(aggmod.Aggregator)
        obj.last_rfid_id = 10
        obj.sessionizer = StrictSessionizer(35, 900, 2)
        return obj

    def test_invalid_row_advances_batch_watermark(self):
        now = datetime(2026, 7, 14, 10, 0, 0)
        rows = [
            (11, now, 1, -50.0, "", "", now, "HOST_CAPTURE_TIME", "epoch-a"),
            (12, now, 1, -50.0, "E" * 24, "T" * 8, now, "HOST_CAPTURE_TIME", "epoch-a"),
        ]
        obj = self.make_aggregator()
        reads, watermark, errors = obj.fetch_reads(FakeConn(rows))
        self.assertEqual(watermark, 12)
        self.assertEqual([r.id for r in reads], [12])
        self.assertEqual(errors, [(11, "EMPTY_EPC")])

    def test_active_memory_can_be_rolled_back_after_db_failure(self):
        obj = self.make_aggregator()
        first = RfidRead(1, datetime(2026, 7, 14, 10, 0, 0), 1, -45, "E" * 24, "T" * 8)
        obj.sessionizer.process(first)
        snapshot = obj.snapshot_active()
        second = RfidRead(2, datetime(2026, 7, 14, 10, 1, 0), 1, -45, "E" * 24, "T" * 8)
        obj.sessionizer.process(second)
        obj.restore_active_snapshot(snapshot)
        restored = next(iter(obj.sessionizer.active.values()))
        self.assertEqual([r.id for r in restored.reads], [1])

    def test_old_backlog_is_not_closed_by_wall_clock_while_rows_arrive(self):
        obj = self.make_aggregator()
        now = datetime(2026, 7, 14, 12, 0, 0)
        old = RfidRead(21, now - timedelta(hours=3), 1, -45, "A" * 24, "1" * 8)
        obj.sessionizer.process(old)
        closed = obj.close_ready_sessions([old], aggmod.Config.RFID_BATCH_SIZE, now)
        self.assertEqual(closed, [])
        self.assertEqual(len(obj.sessionizer.active), 1)

    def test_event_time_watermark_closes_older_session_during_backlog(self):
        obj = self.make_aggregator()
        now = datetime(2026, 7, 14, 12, 0, 0)
        older = RfidRead(31, now - timedelta(hours=3), 1, -45, "A" * 24, "1" * 8)
        newer = RfidRead(32, now - timedelta(hours=2, minutes=58), 1, -45, "B" * 24, "2" * 8)
        obj.sessionizer.process(older)
        obj.sessionizer.process(newer)
        closed = obj.close_ready_sessions([older, newer], aggmod.Config.RFID_BATCH_SIZE, now)
        self.assertEqual([s.full_tag for s in closed], [older.full_tag])
        self.assertIn(newer.full_tag, obj.sessionizer.active)

    def test_quiet_backlog_is_drained_with_explicit_warning(self):
        obj = self.make_aggregator()
        now = datetime(2026, 7, 14, 12, 0, 0)
        old = RfidRead(41, now - timedelta(hours=3), 1, -45, "C" * 24, "3" * 8)
        obj.sessionizer.process(old)
        obj.last_raw_delivery_at = now - timedelta(seconds=aggmod.Config.BACKLOG_QUIET_CLOSE_SEC + 1)
        closed = obj.close_ready_sessions([], 0, now)
        self.assertEqual(len(closed), 1)
        self.assertIn("RFID_BACKLOG_QUIET_CLOSE", closed[0].warning_flags)
        self.assertEqual(obj.sessionizer.active, {})

    def test_recheck_filters_interleaved_foreign_rfid_rows(self):
        now = datetime(2026, 7, 14, 10, 0, 0)
        expected = "A" * 24 + "1" * 8
        rows = [
            (100, now, 1, -40.0, "A" * 24, "1" * 8, now, "HOST_CAPTURE_TIME", "epoch"),
            (101, now + timedelta(milliseconds=10), 2, -41.0, "B" * 24, "2" * 8, now, "HOST_CAPTURE_TIME", "epoch"),
            (102, now + timedelta(milliseconds=20), 2, -42.0, "A" * 24, "1" * 8, now, "HOST_CAPTURE_TIME", "epoch"),
        ]
        restored = aggmod.Aggregator.build_recheck_session(expected, rows, "TIMEOUT")
        self.assertIsNotNone(restored)
        self.assertEqual([r.id for r in restored.reads], [100, 102])
        self.assertEqual(restored.full_tag, expected)
        self.assertEqual(restored.close_reason, "TIMEOUT")

    def test_existing_passage_group_key_is_preserved(self):
        now = datetime(2026, 7, 14, 10, 0, 0)
        s1 = aggmod.TagSession("A" * 32, "A" * 24, "A" * 8, now, now)
        s1.add(RfidRead(201, now, 1, -40, "A" * 24, "A" * 8))
        s2 = aggmod.TagSession("B" * 32, "B" * 24, "B" * 8, now, now)
        s2.add(RfidRead(202, now + timedelta(seconds=1), 1, -40, "B" * 24, "B" * 8))
        groups = aggmod.group_reel_sessions(
            [s1, s2], {s1.event_key: aggmod.Direction.IN, s2.event_key: aggmod.Direction.IN}, 2
        )
        preserved = aggmod.Aggregator.preserve_passage_group_keys(
            groups, {s1.event_key: "f" * 32}, {s1.event_key: aggmod.Direction.IN, s2.event_key: aggmod.Direction.IN}
        )
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].group_key, "f" * 32)
        self.assertEqual(preserved[0].reel_count, 2)


if __name__ == "__main__":
    unittest.main()
