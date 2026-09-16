from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from common.warehouse_report import build_report_records


class WarehouseReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 9, 16)
        self.end = self.start + timedelta(days=1)
        self.dt = self.start + timedelta(hours=9)
        self.tag_a = "A" * 24 + "1" * 8
        self.tag_b = "B" * 24 + "2" * 8
        self.guid = "11111111-2222-3333-4444-555555555555"

    def warehouse(self, **overrides):
        row = {
            "WarehouseId": 101,
            "WarehouseDt": self.dt,
            "WarehouseTag": None,
            "WarehouseDocIds": self.guid,
            "WarehouseSeriesNumber": "7734/26",
        }
        row.update(overrides)
        return row

    def task(self, **overrides):
        row = {
            "Id": 201,
            "Dt": self.dt,
            "Tag": self.tag_a,
            "Ids": self.guid,
            "SeriesNumber": "7734/26",
        }
        row.update(overrides)
        return row

    def event(self, event_id, minutes=0, **overrides):
        row = {
            "EventId": event_id,
            "EventKey": str(event_id),
            "SourceTag": self.tag_a,
            "FirstSeen": self.dt + timedelta(minutes=minutes),
            "LastSeen": self.dt + timedelta(minutes=minutes),
            "WarehouseId": None,
            "FinalDirection": "UNKNOWN",
            "Task1CId": None,
            "Task1CDt": None,
            "Task1CDocIds": "",
            "Task1CSeriesNumber": "",
        }
        row.update(overrides)
        return row

    def test_nullable_tag_matches_kpp_through_ids(self):
        rows = build_report_records(
            [self.warehouse()], [self.task()], [self.event(301)], self.start, self.end
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["EventId"], 301)
        self.assertEqual(rows[0]["WarehouseMatchMethod"], "IDS")
        self.assertEqual(rows[0]["WarehouseLinkStatus"], "MATCH_IDS")
        self.assertEqual(rows[0]["FinalDirection"], "OUT")

    def test_persisted_warehouse_link_wins_over_closer_unlinked_event(self):
        linked = self.event(311, minutes=120, WarehouseId=101, Task1CDocIds=self.guid)
        closer = self.event(312, minutes=5)
        rows = build_report_records(
            [self.warehouse()], [self.task()], [linked, closer], self.start, self.end
        )
        warehouse_row = next(row for row in rows if row.get("WarehouseId") == 101)
        self.assertEqual(warehouse_row["EventId"], 311)
        self.assertEqual(warehouse_row["WarehouseMatchMethod"], "IDS")
        self.assertEqual(
            [row["EventId"] for row in rows if row["WarehouseLinkStatus"] == "KPP_ONLY"],
            [312],
        )

    def test_event_linked_to_another_warehouse_is_not_stolen(self):
        occupied = self.event(321, WarehouseId=999)
        rows = build_report_records(
            [self.warehouse()], [self.task()], [occupied], self.start, self.end
        )
        own = next(row for row in rows if row.get("WarehouseId") == 101)
        self.assertEqual(own["SessionCloseReason"], "WAREHOUSE_ONLY")
        self.assertEqual(own["WarehouseMatchMethod"], "IDS")

    def test_ambiguous_series_stays_warehouse_only(self):
        warehouse = self.warehouse(WarehouseDocIds="99999999-2222-3333-4444-555555555555")
        tasks = [
            self.task(Ids="A", Tag=self.tag_a),
            self.task(Id=202, Ids="B", Tag=self.tag_b),
        ]
        rows = build_report_records([warehouse], tasks, [], self.start, self.end)
        self.assertEqual(rows[0]["WarehouseLinkStatus"], "AMBIGUOUS_SERIES")
        self.assertEqual(rows[0]["WarehouseMatchMethod"], "AMBIGUOUS_SERIES")
        self.assertEqual(rows[0]["SourceTag"], "")


if __name__ == "__main__":
    unittest.main()
