from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from common.warehouse_identity import (
    IdentityRecord,
    MATCH_AMBIGUOUS_SERIES,
    MATCH_IDS,
    MATCH_NONE,
    MATCH_SERIES,
    MATCH_TAG,
    resolve_warehouse_identity,
)


class WarehouseIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dt = datetime(2026, 9, 16, 9, 0, 0)
        self.tag_a = "A" * 24 + "1" * 8
        self.tag_b = "B" * 24 + "2" * 8

    def task(self, row_id, tag, ids, series, minutes=0):
        return IdentityRecord.from_values(
            row_id, self.dt + timedelta(minutes=minutes), tag, ids, series
        )

    def test_nullable_tag_resolves_through_required_ids(self):
        result = resolve_warehouse_identity(
            None,
            "SERIES-GUID-1",
            "7734/26",
            self.dt,
            [self.task(10, self.tag_a, "series-guid-1", "7734/26")],
        )
        self.assertEqual(result.primary_method, MATCH_IDS)
        self.assertEqual(result.preferred_tag, self.tag_a)
        self.assertEqual([(c.method, c.tag) for c in result.candidates], [(MATCH_IDS, self.tag_a)])

    def test_direct_tag_has_priority_over_ids_and_series(self):
        result = resolve_warehouse_identity(
            self.tag_a,
            "ID-1",
            "7734/26",
            self.dt,
            [
                self.task(1, self.tag_a, "OTHER-ID", "OTHER"),
                self.task(2, self.tag_b, "ID-1", "7734/26"),
            ],
        )
        self.assertEqual(result.primary_method, MATCH_TAG)
        self.assertEqual(result.candidates[0].method, MATCH_TAG)
        self.assertEqual(result.candidates[0].tag, self.tag_a)
        self.assertEqual(result.candidates[1].method, MATCH_IDS)

    def test_unique_series_is_last_resort(self):
        result = resolve_warehouse_identity(
            "",
            "MISSING-ID",
            "7734/26",
            self.dt,
            [
                self.task(1, self.tag_a, "OLD-1", "7734/26", -5),
                self.task(2, self.tag_a, "OLD-2", "7734/26", 5),
            ],
        )
        self.assertEqual(result.primary_method, MATCH_SERIES)
        self.assertFalse(result.series_ambiguous)
        self.assertEqual(result.preferred_tag, self.tag_a)

    def test_ambiguous_series_is_not_auto_joined(self):
        result = resolve_warehouse_identity(
            None,
            "MISSING-ID",
            "7734/26",
            self.dt,
            [
                self.task(1, self.tag_a, "OLD-1", "7734/26"),
                self.task(2, self.tag_b, "OLD-2", "7734/26"),
            ],
        )
        self.assertTrue(result.series_ambiguous)
        self.assertEqual(result.primary_method, MATCH_AMBIGUOUS_SERIES)
        self.assertEqual(result.candidates, ())

    def test_empty_values_never_match(self):
        result = resolve_warehouse_identity(
            None,
            "",
            "",
            self.dt,
            [self.task(1, self.tag_a, "", "")],
        )
        self.assertEqual(result.primary_method, MATCH_NONE)
        self.assertEqual(result.candidates, ())

    def test_out_of_window_task_is_not_used(self):
        result = resolve_warehouse_identity(
            None,
            "ID-1",
            "7734/26",
            self.dt,
            [self.task(1, self.tag_a, "ID-1", "7734/26", 24 * 60 + 1)],
        )
        self.assertEqual(result.primary_method, MATCH_NONE)


if __name__ == "__main__":
    unittest.main()
