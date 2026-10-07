from __future__ import annotations

import base64
import importlib
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from common.video_images import decode_video_image


JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/"
    "2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/"
    "8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/"
    "8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDkKKKK908U/9k="
)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class VideoImageTests(unittest.TestCase):
    def test_current_odbc_binary_values(self):
        for value in (JPEG, bytearray(JPEG), memoryview(JPEG)):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(decode_video_image(value, PNG), (JPEG, "image/jpeg"))

    def test_legacy_binary_and_base64_encodings(self):
        encoded = base64.b64encode(JPEG)
        for value in (JPEG, encoded, encoded.decode(), "\n" + encoded.decode() + "\r\n",
                      encoded.decode().replace("/", "\\/"), "data:image/jpeg;base64," + encoded.decode(),
                      "0x" + JPEG.hex()):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(decode_video_image(None, value), (JPEG, "image/jpeg"))

    def test_invalid_new_image_falls_back_to_valid_legacy_image(self):
        self.assertEqual(decode_video_image(b"not a JPEG", memoryview(PNG)), (PNG, "image/png"))
        self.assertEqual(decode_video_image("corrupted", PNG), (PNG, "image/png"))

    def test_missing_or_non_image_payload_is_not_served(self):
        for value in (None, "", "broken%%%", b"\xffbroken", "data:text/plain,hello", base64.b64encode(b"<html>")):
            with self.subTest(value=repr(value)):
                self.assertIsNone(decode_video_image(value, None))


class FakeConnection:
    def __init__(self, results):
        self.results = iter(results)
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self

    def execute(self, query, *params):
        self.executions.append((query, params))
        columns, self.data = next(self.results)
        self.description = [(name,) for name in columns]
        return self

    def fetchone(self):
        return self.data[0] if self.data else None

    def fetchall(self):
        return self.data


class WebSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web_dir = str(Path(__file__).resolve().parents[1] / "web")
        sys.path.insert(0, web_dir)
        try:
            cls.web = importlib.import_module("kpp_reel_dashboard_v3_fixed")
        finally:
            sys.path.remove(web_dir)
        cls.base = cls.web.base

    def setUp(self):
        auth = patch.object(self.base.Config, "AUTH_REQUIRED", False)
        auth.start()
        self.addCleanup(auth.stop)
        self.client = self.base.app.test_client()

    def image_response(self, row):
        conn = FakeConnection([(["ImageData", "ImageBase64"], [] if row is None else [row])])
        with patch.object(self.base, "db_connect", return_value=conn):
            response = self.client.get("/api/image/920649")
        self.assertEqual(conn.executions[0][1], (920649,))
        self.assertTrue(conn.executions[0][0].lstrip().startswith("SELECT"))
        return response

    def test_http_returns_current_yolo_jpeg_unchanged(self):
        response = self.image_response((memoryview(JPEG), None))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, JPEG)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_http_decodes_legacy_base64_bytes_and_actual_mime(self):
        response = self.image_response((None, base64.b64encode(PNG)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, PNG)
        self.assertEqual(response.mimetype, "image/png")

    def test_http_falls_back_from_corrupted_new_value(self):
        response = self.image_response(("damaged", JPEG))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, JPEG)

    def test_absent_or_corrupted_image_has_uncached_404(self):
        for row in (None, (None, None), (b"not an image", "invalid base64")):
            with self.subTest(row=row):
                response = self.image_response(row)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_image_endpoint_requires_same_auth_as_dashboard(self):
        with patch.object(self.base.Config, "AUTH_REQUIRED", True), patch.object(self.base, "db_connect") as db:
            response = self.client.get("/api/image/920649")
        self.assertEqual(response.status_code, 401)
        db.assert_not_called()

    def test_image_query_uses_configured_video_source(self):
        conn = FakeConnection([([], [(JPEG, None)])])
        with patch.object(self.base.Config, "VIDEO_TABLE", "dbo.FactoryVideo"), patch.object(self.base, "db_connect", return_value=conn):
            response = self.client.get("/api/image/920649")
        self.assertEqual(response.status_code, 200)
        self.assertIn("FROM dbo.FactoryVideo WHERE Id=?", conn.executions[0][0])

    def test_production_report_keeps_video_id_for_warehouse_match(self):
        dt = datetime(2026, 10, 7, 7, 54)
        event = {"EventId": 71746, "SourceTag": "A" * 48, "FirstSeen": dt, "LastSeen": dt,
                 "WarehouseId": 4411, "FinalDirection": "IN", "Task1CDocIds": "doc", "VideoEventId": 920649}
        conn = FakeConnection([
            (["WarehouseId", "WarehouseDt", "WarehouseTag", "WarehouseDocIds", "WarehouseSeriesNumber"],
             [(4411, dt, None, "doc", "1234/26")]),
            (["Id", "Dt", "Tag", "Ids", "SeriesNumber"], [(144379, dt, "A" * 48, "doc", "1234/26")]),
            (list(event), [tuple(event.values())]),
        ])
        with patch.object(self.base, "db_connect", return_value=conn):
            rows = self.web.fetch_report_records(date(2026, 10, 7), date(2026, 10, 7))
        self.assertEqual(rows[0]["VideoEventId"], 920649)
        self.assertIn("e.VideoEventId", conn.executions[2][0])
        html = self.base.report_preview_html(rows, date(2026, 10, 7), date(2026, 10, 7))
        self.assertIn('src="/api/image/920649"', html)
        self.assertIn('loading="lazy"', html)
        self.assertTrue(rows[0]["WarehouseDirectionConflict"])
        self.assertIn("Расхождение направления со складом", html)

    def test_details_show_legacy_direction_mismatch_without_sql_writes(self):
        event = {"EventId": 71746, "WarehouseId": 4411, "FinalDirection": "IN",
                 "ConfidencePct": 70, "WarningFlags": "OUT_CONFIRMED_BY_WAREHOUSE"}
        conn = FakeConnection([(list(event), [tuple(event.values())])])
        with patch.object(self.base, "db_connect", return_value=conn):
            response = self.client.get("/api/event/71746")
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["FinalDirection"], "IN")
        self.assertEqual(result["ConfidencePct"], 70)
        self.assertTrue(result["WarehouseDirectionConflict"])
        self.assertEqual(len(conn.executions), 1)
        self.assertTrue(conn.executions[0][0].lstrip().startswith("SELECT"))

    def test_warehouse_only_report_does_not_invent_a_video(self):
        html = self.base.report_preview_html(
            [{"EventId": -4411, "FirstSeen": datetime(2026, 10, 7), "FinalDirection": "OUT", "SourceTag": ""}],
            date(2026, 10, 7), date(2026, 10, 7),
        )
        self.assertNotIn("/api/image/", html)


if __name__ == "__main__":
    unittest.main()
