import tempfile
import time
import unittest
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from common.fallback_store import FallbackStore, metadata_only
from common.replicated_ingest import ReplicatedDelivery


def payload():
    return dict(client_uuid="11111111-1111-1111-1111-111111111111",
                source_time="2026-10-08T12:00:00.123456", source_sequence=42,
                connection_epoch="original-reader-session", antenna=2, rssi=-52.0,
                epc="A" * 24, tid="B" * 24, time_quality="SOURCE_TIME")


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fallback.sqlite"
        self.store = FallbackStore(self.path)

    def test_metadata_drops_photos_recursively_but_preserves_identity_and_time(self):
        data = dict(payload(), photo=b"jpeg", image_base64="secret-image",
                    nested=dict(snapshot_path="photo.jpg", count=2), array=[b"bytes", 3])
        cleaned = metadata_only(data)
        self.assertNotIn("photo", cleaned)
        self.assertNotIn("image_base64", cleaned)
        self.assertEqual({"count": 2}, cleaned["nested"])
        self.assertEqual([3], cleaned["array"])
        self.assertEqual(payload()["source_time"], cleaned["source_time"])

    def test_fallback_only_delivery_remains_pending_until_primary_commit(self):
        delivery = ReplicatedDelivery(fallback=self.store)
        record = delivery.ensure("rfid", payload())
        self.assertEqual(1, self.store.stats()["pending"])
        reopened = FallbackStore(self.path)
        self.assertEqual(payload(), reopened.page()[0]["payload"])
        delivery.committed(record)
        self.assertEqual(0, reopened.stats()["pending"])
        self.assertEqual(1, reopened.stats()["records"])

    def test_idempotent_uuid_rejects_changed_content(self):
        row = self.store.put("rfid", "same", payload())
        self.assertEqual(row, self.store.put("rfid", "same", payload()))
        with self.assertRaisesRegex(ValueError, "Conflict"):
            self.store.put("rfid", "same", dict(payload(), source_sequence=43))
        self.assertEqual(1, self.store.stats()["records"])

    def test_retention_is_at_least_90_days_and_never_deletes_pending(self):
        self.assertEqual(93, FallbackStore(self.path, retention_days=1).retention_days)
        old = time.time() - 94 * 86400
        with patch("common.fallback_store.time.time", return_value=old):
            sent = self.store.put("rfid", "sent", payload())
            self.store.put("rfid", "pending", payload())
        self.store.mark_committed("rfid", "sent", sent["digest"])
        self.assertEqual(1, self.store.maintenance())
        self.assertEqual(["pending"], [x["uuid"] for x in self.store.page(pending=False)])

    def test_failed_replay_keeps_pending_and_successful_retry_is_once(self):
        self.store.put("rfid", "pending", payload())
        def fail(_record):
            raise TimeoutError("PrimaryDatabaseUnavailable")
        with self.assertRaises(TimeoutError):
            self.store.replay(fail)
        self.assertEqual(1, self.store.stats()["pending"])
        called = []
        self.assertEqual(1, self.store.replay(called.append))
        self.assertEqual(0, self.store.replay(called.append))
        self.assertEqual(1, len(called))

    def test_disabled_archive_preserves_existing_delivery_contract(self):
        self.assertIsNone(ReplicatedDelivery().ensure("rfid", payload()))

    def test_archive_commit_failure_does_not_remove_primary_committed_input_from_retry(self):
        from RFID_reader_v4.rfid_to_sql_v4 import Spool, SQLWriter
        spool = Spool(str(self.path.parent / "reader.sqlite"))
        spool.enqueue(payload())
        with patch.dict("os.environ", {"PERIMETER_REPLICA_ENABLED": "0", "PERIMETER_FALLBACK_ENABLED": "0"}):
            writer = SQLWriter(spool)
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.commit.side_effect = writer.stop
        writer.replication = Mock()
        writer.replication.committed.side_effect = OSError("archive-write-failed")
        with patch.dict(sys.modules, {"pyodbc": types.SimpleNamespace(connect=lambda *_args, **_kwargs: connection)}):
            writer.run()
        self.assertEqual((1, 0), spool.stats()[:2])
        self.assertEqual(1, writer.failed_total)


if __name__ == "__main__":
    unittest.main()
