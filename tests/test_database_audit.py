import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from tools.audit_database_export import FIELDS, audit


class DatabaseAuditTests(unittest.TestCase):
    def source(self, rows):
        values = {table: rows.get(table, []) for table in FIELDS}
        return SimpleNamespace(rows=lambda table: iter(values[table]),
                               inventory={table: {"rows": len(items)} for table, items in values.items()})

    def test_empty_link_identities_do_not_create_shared_or_duplicate_passages(self):
        rows = {"KPP_EventVideoLinks": [dict(PassageGroupKey="", VideoEventId="8", ReelCount="1", LinkedAt="")] * 2,
                "KPP_EventSkudLinks": [dict(PassageGroupKey="", SkudExternalId="9", LinkedAt=""),
                                       dict(PassageGroupKey="valid", SkudExternalId="", LinkedAt="")]}
        with patch("tools.audit_database_export.Export", return_value=self.source(rows)):
            result = audit("unused", datetime(2026, 10, 6))
        self.assertEqual(0, result["groups_with_both_video_and_skud_links"])
        for value in result["links"].values():
            self.assertEqual(2, value["missing_keys"])
            self.assertEqual(0, value["pair_duplicates"])
            self.assertEqual(0, value["distinct_passage_groups"])

    def test_aggregate_warehouse_result_contains_no_record_examples(self):
        rows = {"Warehouse": [dict(Id="987654321", Dt="2026-10-07T12:34:56", Tag="", Ids="doc", SeriesNumber="series")]}
        with patch("tools.audit_database_export.Export", return_value=self.source(rows)):
            result = audit("unused", datetime(2026, 10, 6))
        self.assertNotIn("recent_candidate_examples", result["warehouse"])
        self.assertNotIn("987654321", json.dumps(result))
        self.assertEqual(1, result["warehouse"]["rows_since_hotfix"])
