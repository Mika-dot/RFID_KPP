import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from common.metadata_mirror import MetadataMirror
from observer.mirror import EVENT_FIELDS, STREAMS, REQUIRED_STREAMS, MirrorReadBlocked, sync_metadata


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "metadata.sqlite"
        self.mirror = MetadataMirror(self.path, retention_days=1)
        self.now = datetime.now()

    def test_blocked_event_anchor_preserves_cursor_and_syncs_other_streams(self):
        self.mirror.commit_batch("events", [(10, self.now, {"EventId": 10})],
                                 [self.now.isoformat(), 10], True)
        before = self.mirror.stats()["streams"]["events"]
        empty = Mock(); empty.fetchall.return_value = []
        blocked = Exception("42000", "[SQL Server]lock timeout (1222) (SQLExecDirectW)")
        connection = Mock()
        connection.execute.side_effect = [Mock(), blocked] + [empty] * 5
        with patch.dict("os.environ", {"KPP_TASK_CONN_STR": "", "PERIMETER_OBSERVER_TASK_SQL": ""}):
            with self.assertRaises(MirrorReadBlocked) as failure:
                sync_metadata(self.mirror, connection)
        self.assertEqual(("events",), failure.exception.streams)
        self.assertEqual([self.now.isoformat(), 10], self.mirror.watermark("events"))
        after = self.mirror.stats()["streams"]["events"]
        for key in ("synced_at", "caught_up", "records"):
            self.assertEqual(before[key], after[key])
        self.assertGreaterEqual(after["age_sec"], before["age_sec"])
        self.assertEqual(10, self.mirror.recent()[0]["EventId"])
        self.assertEqual(set(REQUIRED_STREAMS), set(self.mirror.stats()["streams"]))
        connection.close.assert_called_once()

    def test_blocked_fetch_does_not_claim_freshness_and_retries_same_cursor(self):
        cursor = Mock()
        cursor.fetchall.side_effect = Exception("42000", "lock request timeout (1222)")
        empty = Mock(); empty.fetchall.return_value = []
        connection = Mock()
        connection.execute.side_effect = [Mock(), cursor] + [empty] * 5
        with patch.dict("os.environ", {"KPP_TASK_CONN_STR": "", "PERIMETER_OBSERVER_TASK_SQL": ""}):
            with self.assertRaises(MirrorReadBlocked):
                sync_metadata(self.mirror, connection)
            self.assertIsNone(self.mirror.watermark("events"))
            connection.execute.side_effect = None
            connection.execute.return_value = empty
            sync_metadata(self.mirror, connection)
        self.assertEqual(["1900-01-01T00:00:00", 0], self.mirror.watermark("events"))

    def test_other_programming_error_is_not_silently_skipped(self):
        missing = Exception("42000", "Invalid column name (207)")
        connection = Mock(); connection.execute.side_effect = [Mock(), missing]
        with patch.dict("os.environ", {"KPP_TASK_CONN_STR": "", "PERIMETER_OBSERVER_TASK_SQL": ""}):
            with self.assertRaisesRegex(Exception, "Invalid column"):
                sync_metadata(self.mirror, connection)
        self.assertEqual({}, self.mirror.stats()["streams"])
        connection.close.assert_called_once()

    def test_guardian_reports_blocked_stream_and_keeps_recent_events_stale(self):
        import threading
        from guardian.node import Node
        node = Node.__new__(Node)
        node.cfg = {"node_id": "comparator"}
        node.mirror, node.lock = self.mirror, threading.RLock()
        node.store, node.telemetry = Mock(), Mock()
        with patch("observer.mirror.sync_metadata", side_effect=MirrorReadBlocked(["events"])):
            node.sync_mirror()
        self.assertEqual("MirrorReadBlocked", node.mirror_status["error"])
        self.assertEqual(["events"], node.mirror_status["blocked_streams"])
        node.telemetry.event.assert_called_once_with("mirror_sync_blocked", streams=["events"], native_code=1222)
        self.assertTrue(node.recent_events()["stale"])

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
        empty = Mock(); empty.fetchall.return_value = []
        responses += [empty, empty]
        connection.execute.side_effect = responses
        result = sync_metadata(self.mirror, connection, batch_size=100)
        self.assertEqual(set(REQUIRED_STREAMS), set(result))
        self.assertTrue(all(value == 1 for value in result.values()))
        sql = " ".join(str(call.args[0]) for call in connection.execute.call_args_list)
        for excluded in ("ImageData", "ImageBase64", "RawData", "FullName", "CardNumReal"):
            self.assertNotIn(excluded, sql)
        self.assertEqual(8, sql.count("TOP (100)"))
        self.assertEqual([10], [row["ExternalId2"] for row in self.mirror.recent("skud")])

    def test_existing_warehouse_identity_is_refreshed_without_advancing_ingest_cursor(self):
        self.mirror.commit_batch("warehouse", [(10, self.now, {"Id":10,"Tag":None})], 10, True)
        def execute(sql, *args):
            cursor = Mock(); cursor.fetchall.return_value = []
            cursor.fetchone.return_value = (10,)
            if "dbo.Warehouse" in sql and "Id<=?" in sql:
                cursor.fetchall.return_value = [(10, self.now, "corrected-tag", "corrected-id", "corrected-series")]
            return cursor
        connection = Mock(); connection.execute.side_effect = execute
        with patch.dict("os.environ", {"KPP_TASK_CONN_STR":"", "PERIMETER_OBSERVER_TASK_SQL":""}):
            sync_metadata(self.mirror, connection)
        self.assertEqual("corrected-tag", self.mirror.lookup("warehouse",10)["Tag"])
        self.assertEqual(10, self.mirror.watermark("warehouse"))
        self.assertEqual(0, self.mirror.refresh_position("warehouse"))
        self.assertIsNotNone(self.mirror.stats()["registry_refresh"]["warehouse"]["completed_at"])

    def test_registry_refresh_resumes_durably_and_cycles_after_last_page(self):
        self.mirror.commit_batch("tasks", [(3,self.now,{"Id":3,"Tag":"old"})], 3, True)
        self.mirror.commit_refresh("tasks", [(1,self.now,{"Id":1,"Tag":"updated"})], 1, False)
        restarted = MetadataMirror(self.path)
        self.assertEqual(1, restarted.refresh_position("tasks"))
        self.assertEqual(3, restarted.watermark("tasks"))
        restarted.commit_refresh("tasks", [(3,self.now,{"Id":3,"Tag":"corrected"})], 0, True)
        self.assertEqual(0, restarted.refresh_position("tasks"))
        self.assertEqual("corrected", restarted.lookup("tasks",3)["Tag"])

    def test_completed_registry_refresh_removes_source_deletions_only_after_full_cycle(self):
        self.mirror.commit_batch("warehouse", [
            (1,self.now,{"Id":1,"Tag":"keep"}),
            (2,self.now,{"Id":2,"Tag":"deleted"}),
            (3,self.now,{"Id":3,"Tag":"keep"})], 3, True)
        self.mirror.commit_refresh("warehouse", [(1,self.now,{"Id":1,"Tag":"keep"})],
                                   1, False, 3)
        self.assertEqual("deleted", self.mirror.lookup("warehouse",2)["Tag"])
        restarted = MetadataMirror(self.path)
        restarted.commit_refresh("warehouse", [(3,self.now,{"Id":3,"Tag":"keep"})],
                                 0, True, 3)
        self.assertIsNone(restarted.lookup("warehouse",2))
        self.assertEqual("keep", restarted.lookup("warehouse",1)["Tag"])
        self.assertEqual("keep", restarted.lookup("warehouse",3)["Tag"])

    def test_upgrade_restarts_legacy_partial_refresh_without_deleting_unseen_rows(self):
        self.mirror.commit_batch("warehouse", [
            (1,self.now,{"Id":1,"Tag":"first"}),
            (2,self.now,{"Id":2,"Tag":"second"})], 2, True)
        db = sqlite3.connect(self.path)
        try:
            db.execute("INSERT INTO registry_refresh VALUES('warehouse',1,NULL)")
            db.execute("DROP TABLE registry_refresh_seen")
            db.execute("DROP TABLE registry_refresh_cycles")
            db.commit()
        finally:
            db.close()
        upgraded = MetadataMirror(self.path)
        self.assertEqual(0, upgraded.refresh_position("warehouse"))
        upgraded.commit_refresh("warehouse", [
            (1,self.now,{"Id":1,"Tag":"first"}),
            (2,self.now,{"Id":2,"Tag":"second"})], 0, True, 2)
        self.assertEqual("first", upgraded.lookup("warehouse",1)["Tag"])
        self.assertEqual("second", upgraded.lookup("warehouse",2)["Tag"])

    def test_lock_in_registry_refresh_keeps_refresh_cursor_and_old_payload(self):
        self.mirror.commit_batch("warehouse", [(10,self.now,{"Id":10,"Tag":"old"})], 10, True)
        self.mirror.commit_refresh("warehouse", [], 2, False)
        def execute(sql,*args):
            if "dbo.Warehouse" in sql and "Id<=?" in sql:
                raise Exception("42000", "lock request timeout (1222)")
            cursor=Mock();cursor.fetchone.return_value=(10,);cursor.fetchall.return_value=[]
            return cursor
        connection=Mock();connection.execute.side_effect=execute
        with patch.dict("os.environ", {"KPP_TASK_CONN_STR":"", "PERIMETER_OBSERVER_TASK_SQL":""}):
            with self.assertRaises(MirrorReadBlocked):sync_metadata(self.mirror,connection)
        self.assertEqual(2,self.mirror.refresh_position("warehouse"))
        self.assertEqual("old",self.mirror.lookup("warehouse",10)["Tag"])

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
