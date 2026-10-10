import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from common.metadata_mirror import MetadataMirror
from observer.mirror import EVENT_FIELDS, STREAMS, REQUIRED_STREAMS, sync_metadata


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "metadata.sqlite"
        self.mirror = MetadataMirror(self.path, retention_days=1)
        self.now = datetime.now()

    def test_atomic_batch_reopens_and_updates_metadata_without_photos(self):
        self.mirror.commit_batch("events", [(1, self.now, {"EventId": 1, "ImageData": b"jpeg", "NeedRecheck": 1})], ["first", 1])
        restored = MetadataMirror(self.path)
        self.assertEqual(["first", 1], restored.watermark("events"))
        self.assertNotIn("ImageData", restored.recent()[0])
        restored.commit_batch("events", [(1, self.now, {"EventId": 1, "NeedRecheck": 0})], ["next", 1], True)
        self.assertEqual(1, len(restored.recent()))
        self.assertEqual(0, restored.recent()[0]["NeedRecheck"])

    def test_invalid_batch_never_advances_watermark_or_partially_inserts(self):
        with self.assertRaises(ValueError):
            self.mirror.commit_batch("events", [(1, self.now, {}), (2, "bad-time", {})], ["next", 2])
        self.assertEqual([], self.mirror.recent())
        self.assertIsNone(self.mirror.watermark("events"))

    def test_retention_and_daily_counts_do_not_count_warehouse_as_reader_out(self):
        records = [(1, self.now - timedelta(days=94), {"old": True}),
                   (2, self.now, {"RfidReadCount": 10, "IsReel": 1, "FinalDirection": "IN"}),
                   (3, self.now, {"RfidReadCount": 0, "IsReel": 1, "FinalDirection": "OUT", "SessionCloseReason": "WAREHOUSE_ONLY"})]
        self.mirror.commit_batch("events", records, 3, True)
        self.assertEqual(93, self.mirror.retention_days)
        self.assertEqual(1, self.mirror.maintenance(self.now))
        self.assertEqual({"in_24h": 1, "out_24h": 0, "warehouse_only_24h": 1, "recheck_24h": 0}, self.mirror.counts_24h(self.now))

    def test_sql_sync_is_select_only_and_keeps_safe_watermark_on_source_failure(self):
        row = {key: None for key in EVENT_FIELDS}
        row.update(EventId=10, FirstSeen=self.now, UpdatedAt=self.now, RfidReadCount=2, IsReel=1)
        cursor = Mock()
        cursor.fetchall.return_value = [tuple(row[key] for key in EVENT_FIELDS)]
        connection = Mock()
        connection.execute.side_effect = [Mock(), cursor, TimeoutError("database-down")]
        with self.assertRaises(TimeoutError):
            sync_metadata(self.mirror, connection)
        self.assertEqual([self.now.isoformat(), 10], self.mirror.watermark("events"))
        self.assertIsNone(self.mirror.watermark("warehouse"))
        self.assertEqual(10, self.mirror.recent()[0]["EventId"])
        connection.close.assert_called_once()
        sql = " ".join(str(call.args[0]) for call in connection.execute.call_args_list)
        self.assertNotIn("INSERT", sql)
        self.assertNotIn("UPDATE ", sql)
        self.assertNotIn("ImageData", sql)

    def test_primary_rollback_does_not_refresh_or_reset_local_history(self):
        self.mirror.commit_batch("events", [(10, self.now, {"EventId": 10})],
                                 [self.now.isoformat(), 10], True)
        before = self.mirror.stats()["streams"]["events"]["synced_at"]
        cursor = Mock()
        cursor.fetchone.return_value = None
        connection = Mock()
        connection.execute.side_effect = [Mock(), cursor]
        with self.assertRaisesRegex(RuntimeError, "MirrorSourceHistoryChanged:events"):
            sync_metadata(self.mirror, connection)
        self.assertEqual(before, self.mirror.stats()["streams"]["events"]["synced_at"])
        self.assertEqual(10, self.mirror.recent()[0]["EventId"])
        connection.close.assert_called_once()

    def test_all_six_streams_have_bounded_metadata_without_images_or_personal_fields(self):
        responses = [Mock()]
        for _stream, _env, _table, fields, _id, _time in STREAMS:
            values = [None] * len(fields)
            values[0], values[1] = 10, self.now
            if "UpdatedAt" in fields:
                values[fields.index("UpdatedAt")] = self.now
            cursor = Mock()
            cursor.fetchall.return_value = [tuple(values)]
            responses.append(cursor)
        connection = Mock()
        connection.execute.side_effect = responses
        result = sync_metadata(self.mirror, connection, batch_size=100)
        self.assertEqual(set(REQUIRED_STREAMS), set(result))
        self.assertTrue(all(value == 1 for value in result.values()))
        sql = " ".join(str(call.args[0]) for call in connection.execute.call_args_list)
        for excluded in ("ImageData", "ImageBase64", "RawData", "FullName", "CardNumReal"):
            self.assertNotIn(excluded, sql)
        self.assertEqual(6, sql.count("TOP (100)"))
        self.assertEqual([10], [row["ExternalId2"] for row in self.mirror.recent("skud")])

    def test_empty_event_cursor_can_sync_twice_then_receive_first_event(self):
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = []
        with patch.dict("os.environ", {"KPP_TASK_CONN_STR": "", "PERIMETER_OBSERVER_TASK_SQL": ""}):
            sync_metadata(self.mirror, connection)
            sync_metadata(self.mirror, connection)
        queries = [call.args[0] for call in connection.execute.call_args_list]
        self.assertFalse(any("WHERE EventId=?" in sql for sql in queries))
        row = {key: None for key in EVENT_FIELDS}
        row.update(EventId=1, FirstSeen=self.now, UpdatedAt=self.now)
        cursor = Mock()
        cursor.fetchall.return_value = [tuple(row[key] for key in EVENT_FIELDS)]
        empty = Mock()
        empty.fetchall.return_value = []
        connection.execute.side_effect = [Mock(), cursor] + [empty] * 5
        sync_metadata(self.mirror, connection)
        self.assertEqual(1, self.mirror.recent()[0]["EventId"])

    def test_registries_use_separate_readonly_task_database(self):
        event_conn, task_conn = Mock(), Mock()
        for connection in (event_conn, task_conn):
            connection.execute.return_value.fetchall.return_value = []
        result = sync_metadata(self.mirror, event_conn, task_connection=task_conn)
        self.assertEqual(set(REQUIRED_STREAMS), set(result))
        task_sql = " ".join(call.args[0] for call in task_conn.execute.call_args_list)
        event_sql = " ".join(call.args[0] for call in event_conn.execute.call_args_list)
        for name in ("dbo.Warehouse", "dbo.RfidTags"):
            self.assertIn(name, task_sql)
            self.assertNotIn(name, event_sql)
        self.assertNotIn("dbo.RFID_Tags", task_sql)
        for connection in (event_conn, task_conn):
            connection.close.assert_called_once()

    def test_partial_backfill_is_stale_on_node_recent_endpoint(self):
        from guardian.node import Node
        self.mirror.commit_batch("events", [(1, self.now, {"EventId": 1})], [self.now.isoformat(), 1], False)
        node = Node.__new__(Node)
        node.cfg, node.mirror, node.mirror_status = {"node_id": "physical"}, self.mirror, {}
        self.assertTrue(node.recent_events()["stale"])
        self.assertEqual({}, node.recent_events()["counts"])
        self.mirror.commit_batch("events", [], [self.now.isoformat(), 1], True)
        self.assertFalse(node.recent_events()["stale"])


if __name__ == "__main__":
    unittest.main()
