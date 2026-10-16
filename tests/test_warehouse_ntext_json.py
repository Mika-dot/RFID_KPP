"""Regression coverage for SQL Server 8116 on legacy ntext EvidenceJson.

Offline SQL-shape tests; production SQL Server compatibility must additionally
be confirmed against the deployed database schema before rollout.
"""
from __future__ import annotations

import inspect
import json
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "KPP")]
sys.modules.setdefault("pyodbc", types.SimpleNamespace(Connection=object, Cursor=object, connect=None))

from common.adaptive_windows import AdaptiveWindowModel
from common.warehouse_identity import IdentityCandidate, IdentityResolution, MATCH_TAG
from kpp_aggregator_v3_warehouse import Aggregator
from kpp_aggregator_v3_warehouse_v3 import Aggregator as ProductionAggregator

CAST = "CONVERT(nvarchar(max), EvidenceJson)"
SAFE = "CASE WHEN ISJSON(" + CAST + ")=1 THEN " + CAST + " ELSE N'{}' END"


class RecordingCursor:
    def __init__(self, rows=None):
        self.rows = [] if rows is None else list(rows)
        self.statements = []

    def execute(self, sql, *parameters):
        assert sql.count("?") == len(parameters), (sql, parameters)
        assert "ISJSON(EvidenceJson)" not in sql, sql
        assert "THEN EvidenceJson ELSE" not in sql, sql
        assert "JSON_QUERY(?)" not in sql, sql
        if "EvidenceJson" in sql and any(
            token in sql for token in ("JSON_QUERY(", "JSON_MODIFY(", "JSON_VALUE(")
        ):
            assert SAFE in sql, sql
        self.statements.append((sql, parameters))
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class WarehouseNtextJsonTests(unittest.TestCase):
    dt = datetime(2026, 10, 16, 18, 35, 0)

    def make_agg(self):
        return Aggregator.__new__(Aggregator)

    def test_all_json_expressions_convert_legacy_ntext(self):
        source = inspect.getsource(Aggregator)
        self.assertEqual(source.count("ISJSON(" + CAST + ")"), 6)
        self.assertEqual(source.count("THEN " + CAST + " ELSE N'{{}}'"), 6)
        self.assertNotIn("ISJSON(EvidenceJson)", source)
        self.assertNotIn("THEN EvidenceJson ELSE", source)
        self.assertEqual(source.count("JSON_QUERY(CONVERT(nvarchar(max), ?))"), 2)
        self.assertNotIn("JSON_QUERY(?)", source)

    def test_enrich_existing_event_reads_and_writes_with_safe_cast(self):
        cursor = RecordingCursor(rows=[(None,)])
        agg = self.make_agg()
        agg._enrich_existing_event(
            cursor, 44, 101, self.dt,
            "11111111-2222-3333-4444-555555555555",
            "7734/26", None, MATCH_TAG,
        )
        self.assertEqual(len(cursor.statements), 2)
        self.assertIn("SELECT JSON_QUERY(" + SAFE, cursor.statements[0][0])
        self.assertIn("EvidenceJson=JSON_MODIFY(", cursor.statements[1][0])
        self.assertIn(SAFE, cursor.statements[1][0])
        self.assertIn("JSON_QUERY(CONVERT(nvarchar(max), ?))", cursor.statements[1][0])
        payload = cursor.statements[1][1][-2]
        self.assertEqual(json.loads(payload)["id"], 101)

    def test_large_pyodbc_json_parameter_is_explicitly_converted(self):
        # With long JSON strings pyodbc may bind '?' as SQL_WLONGVARCHAR
        # (ntext), which JSON_QUERY rejects with SQL error 8116.
        cursor = RecordingCursor(rows=[(None,)])
        agg = self.make_agg()
        agg._adaptive_link_evidence = {44: {"audit_note": "x" * 6000}}
        agg._enrich_existing_event(
            cursor, 44, 101, self.dt, "00000000-0000-0000-0000-000000000001",
            "7734/26", None, MATCH_TAG,
        )
        query, params = cursor.statements[-1]
        self.assertGreater(len(params[-2]), 4000)
        self.assertIn("JSON_QUERY(CONVERT(nvarchar(max), ?))", query)
        self.assertEqual(json.loads(params[-2])["audit_note"], "x" * 6000)

    def test_supersede_synthetic_event_uses_safe_cast(self):
        cursor = RecordingCursor()
        self.make_agg()._supersede_warehouse_only(cursor, 101, 44)
        self.assertEqual(len(cursor.statements), 1)
        sql, params = cursor.statements[0]
        self.assertIn("EvidenceJson=JSON_MODIFY(", sql)
        self.assertIn(SAFE, sql)
        self.assertEqual(params, (44, 101, 44))

    def test_adaptive_training_query_uses_safe_cast(self):
        agg = self.make_agg()
        agg.ADAPTIVE_WINDOWS_ENABLED = True
        agg.ADAPTIVE_WINDOWS_MODE = "shadow"
        agg.last_adaptive_refresh = datetime.min
        agg.adaptive_windows = agg._new_adaptive_model()
        cursor = RecordingCursor()
        agg._refresh_adaptive_model(FakeConnection(cursor), force=True)
        self.assertEqual(len(cursor.statements), 1)
        sql = cursor.statements[0][0]
        self.assertEqual(sql.count(SAFE), 2)
        self.assertIn("JSON_VALUE(", sql)

    def test_retrospective_warehouse_only_update_uses_safe_cast(self):
        agg = ProductionAggregator.__new__(ProductionAggregator)
        tag = "E2000020590A02011510B114E20034120133040000A184C3"
        identity = IdentityResolution((IdentityCandidate(tag, MATCH_TAG, None),),
                                      None, MATCH_TAG)
        agg._resolve_identity = lambda *_: identity
        agg._find_existing_linked_event = lambda *_: None
        agg._find_kpp_event = lambda *_: None
        agg._insert_warehouse_only = lambda *_: None
        agg._retrospective_evidence = lambda *_: {"raw_candidates": [], "id": 101}
        cursor = RecordingCursor()
        result = agg._process_warehouse_row(
            cursor, (101, self.dt, tag,
                     "11111111-2222-3333-4444-555555555555", "7734/26")
        )
        self.assertEqual(result, ("warehouse_only", False))
        self.assertEqual(len(cursor.statements), 1)
        sql, params = cursor.statements[0]
        self.assertIn("EvidenceJson=JSON_MODIFY(" + SAFE, sql)
        self.assertIn("JSON_QUERY(CONVERT(nvarchar(max), ?))", sql)
        self.assertEqual(json.loads(params[0])["id"], 101)
        self.assertEqual(params[1], 101)

    def test_malformed_and_null_json_fallback_remains_present(self):
        source = inspect.getsource(Aggregator)
        # ISJSON returns 0 or NULL on invalid/NULL input, so JSON functions
        # must receive a JSON object instead of the legacy invalid column value.
        self.assertIn("ELSE N'{{}}' END", source)
        self.assertEqual(source.count("ISJSON(" + CAST + ")"), 6)


if __name__ == "__main__":
    unittest.main()
