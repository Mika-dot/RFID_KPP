import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tools.build_recorded_traces import build


class RecordedTraceTests(unittest.TestCase):
    def test_source_timing_identity_and_skud_survive_without_personal_or_original_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            epc, tid = "E" * 24, "F" * 24
            tables = {
                "RFID_Tags": [dict(Id=987, RecordTime="2026-10-06T12:01:00", SourceReaderTime="",
                                   Antenna=2, RSSI=-40, EPC=epc, TID=tid),
                              dict(Id=999, RecordTime="2026-10-06T12:01:01.250", SourceReaderTime="",
                                   Antenna=1, RSSI=-44, EPC=epc, TID=tid)],
                "RfidTags": [dict(Id=456, Dt="2026-10-06T12:00:00", Tag=epc + tid,
                                  Ids="real-document", SeriesNumber="real-series")],
                "Warehouse": [dict(Id=789, Dt="2026-10-06T12:03:00", Tag="",
                                   Ids="real-document", SeriesNumber="real-series")],
                "RusGuardLogs": [dict(ExternalId2=555, CreatedAt="2026-10-06T12:01:00", Direction="IN",
                                      FullName="Private Person", CardNumReal="private-card")],
            }
            for table, rows in tables.items():
                with (directory / (table + "_export.csv")).open("w", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                    writer.writeheader(); writer.writerows(rows)
            corpus = build(directory, datetime(2026, 10, 6, 12), datetime(2026, 10, 6, 13), 1, b"test-private-key")
            raw = json.dumps(corpus)
            for private in (epc, tid, "real-document", "real-series", "Private Person", "private-card", "FullName", "CardNumReal"):
                self.assertNotIn(private, raw)
            trace = corpus["traces"][0]
            self.assertEqual([1, 2], [row["id"] for row in trace["reads"]])
            self.assertEqual([0, 1.25], [row["offset"] for row in trace["reads"]])
            self.assertEqual(trace["tasks"][0]["Tag"], trace["reads"][0]["epc"] + trace["reads"][0]["tid"])
            self.assertEqual(trace["tasks"][0]["Ids"], trace["warehouse"][0]["ids"])
            self.assertEqual("", trace["warehouse"][0]["tag"])
            self.assertEqual("IN", trace["skud"][0]["direction"])
            self.assertEqual([], trace["video"])
            self.assertNotIn("expected", trace)


if __name__ == "__main__":
    unittest.main()
