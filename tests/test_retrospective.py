import unittest
from datetime import datetime, timedelta

from common.retrospective import session_candidates


class RetrospectiveTests(unittest.TestCase):
    def test_ranked_raw_rows_restore_production_order_without_confirming_or_training(self):
        warehouse = datetime(2026, 10, 8, 12)
        passage = warehouse - timedelta(minutes=10)
        rows = [(11, passage + timedelta(seconds=1), 1, -40), (10, passage, 2, -42)]
        result = session_candidates("E" * 24 + "F" * 24, warehouse, rows,
                                    passage - timedelta(seconds=30), passage + timedelta(seconds=30),
                                    warehouse - timedelta(days=1), warehouse + timedelta(days=1), {2, 3}, {1, 4})
        self.assertEqual(1, len(result))
        self.assertEqual("IN", result[0]["rfid_direction_candidate"])
        self.assertEqual(2, result[0]["read_count"])
        self.assertFalse(result[0]["learning_eligible"])
        self.assertFalse(result[0]["physical_passage_confirmed"])
        self.assertTrue(result[0]["in_expected_window"])

    def test_recovery_outside_old_window_keeps_distinct_sessions_and_truncation_warning(self):
        warehouse = datetime(2026, 10, 8, 12)
        at = warehouse - timedelta(days=2)
        rows = [(1, at, 1, -40), (2, at + timedelta(seconds=1), 2, -44),
                (3, at + timedelta(minutes=2), 1, -43)]
        result = session_candidates("A" * 48, warehouse, rows,
                                    at - timedelta(seconds=30), at + timedelta(seconds=30),
                                    warehouse - timedelta(days=1), warehouse + timedelta(days=1), {2, 3}, {1, 4}, page_limit=3)
        self.assertEqual(2, len(result))
        self.assertEqual("OUT", result[0]["rfid_direction_candidate"])
        self.assertTrue(all(row["outside_legacy_window"] and row["page_may_be_truncated"] for row in result))


if __name__ == "__main__":
    unittest.main()
