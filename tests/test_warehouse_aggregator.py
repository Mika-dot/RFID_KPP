from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "KPP")]
sys.modules.setdefault("pyodbc", types.SimpleNamespace(Connection=object, Cursor=object, connect=None))

import kpp_aggregator_v3_warehouse as warehouse_aggregator  # noqa: E402
import kpp_aggregator_v3_warehouse_v3 as production_aggregator  # noqa: E402
from common.warehouse_identity import (  # noqa: E402
    IdentityCandidate,
    IdentityRecord,
    IdentityResolution,
    MATCH_IDS,
)


class RecordingCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executions = []

    def execute(self, query, *params):
        if len(params) == 1 and isinstance(params[0], list):
            params = tuple(params[0])
        self.assert_placeholder_count(query, params)
        self.executions.append((query, params))
        return self

    @staticmethod
    def assert_placeholder_count(query, params):
        if query.count("?") != len(params):
            raise AssertionError((query.count("?"), len(params), query))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class WarehouseAggregatorTests(unittest.TestCase):
    def setUp(self):
        self.dt = datetime(2026, 9, 16, 9)
        self.guid = "11111111-2222-3333-4444-555555555555"
        self.tag = "A" * 24 + "1" * 8

    def make_aggregator(self):
        obj = warehouse_aggregator.Aggregator.__new__(warehouse_aggregator.Aggregator)
        obj.WAREHOUSE_RECHECK_SEC = 60
        return obj

    def make_production_aggregator(self):
        obj = production_aggregator.Aggregator.__new__(production_aggregator.Aggregator)
        obj.WAREHOUSE_RECHECK_SEC = 60
        return obj

    def test_uniqueidentifier_query_does_not_apply_string_functions_to_ids(self):
        obj = self.make_aggregator()
        cur = RecordingCursor()
        rows = obj._load_task_candidates(cur, "", self.guid.upper(), "7734/26", self.dt)
        self.assertEqual(rows, [])
        query, params = cur.executions[0]
        self.assertIn("Ids=CONVERT(uniqueidentifier, ?)", query)
        self.assertNotIn("LTRIM(RTRIM(Ids))", query)
        self.assertIn(self.guid, params)

    def test_late_kpp_event_enriches_real_event_and_supersedes_synthetic(self):
        obj = self.make_aggregator()
        task = IdentityRecord.from_values(7, self.dt, self.tag, self.guid, "7734/26")
        resolution = IdentityResolution(
            (IdentityCandidate(self.tag, MATCH_IDS, task),), task, MATCH_IDS, False
        )
        calls = []
        obj._resolve_identity = lambda *_args: resolution
        obj._find_existing_linked_event = lambda *_args: None
        obj._find_kpp_event = lambda *_args: 44
        obj._enrich_existing_event = lambda *args: calls.append(("enrich", args[1]))
        obj._supersede_warehouse_only = lambda *args: calls.append(("supersede", args[1]))
        obj._insert_warehouse_only = lambda *_args: self.fail("must not create another synthetic event")

        outcome, ambiguous = obj._process_warehouse_row(
            RecordingCursor(), (101, self.dt, None, self.guid, "7734/26")
        )
        self.assertEqual(outcome, "enriched")
        self.assertFalse(ambiguous)
        self.assertEqual(calls, [("enrich", 44), ("supersede", 101)])

    def test_warehouse_only_insert_sql_has_complete_parameter_list(self):
        obj = self.make_aggregator()
        cur = RecordingCursor()
        obj._insert_warehouse_only(
            cur,
            18_000_001,
            self.dt,
            "",
            self.guid,
            "7734/26",
            None,
            "NONE",
            "WAREHOUSE_ONLY",
        )
        query, _params = cur.executions[0]
        self.assertIn("NeedRecheck=1", query)
        self.assertIn("NextRecheckAt=DATEADD(second,?", query)

    def test_production_lookup_can_promote_unknown_rfid_event(self):
        obj = self.make_production_aggregator()
        cur = RecordingCursor(rows=[(44,)])
        event_id = obj._find_kpp_event(cur, self.tag, self.dt, 101)
        self.assertEqual(event_id, 44)
        query, _params = cur.executions[0]
        self.assertIn("ISNULL(RfidReadCount,0)>0", query)
        self.assertNotIn("IsReel=1", query)
        self.assertIn("ISNULL(SessionCloseReason,'')<>'WAREHOUSE_ONLY'", query)

    def test_production_existing_link_accepts_unknown_rfid_but_not_synthetic(self):
        obj = self.make_production_aggregator()
        cur = RecordingCursor(rows=[(45, self.tag.lower())])
        linked = obj._find_existing_linked_event(cur, 101, self.dt)
        self.assertEqual(linked, (45, self.tag))
        query, _params = cur.executions[0]
        self.assertIn("ISNULL(RfidReadCount,0)>0", query)
        self.assertNotIn("IsReel=1", query)
        self.assertIn("ISNULL(SessionCloseReason,'')<>'WAREHOUSE_ONLY'", query)


if __name__ == "__main__":
    unittest.main()
