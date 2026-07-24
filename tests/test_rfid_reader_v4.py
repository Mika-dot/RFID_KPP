from __future__ import annotations

import importlib.util
import tempfile
import unittest
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "RFID_reader_v4" / "rfid_to_sql_v4.py"
spec = importlib.util.spec_from_file_location("rfid_reader_v4", MODULE_PATH)
reader = importlib.util.module_from_spec(spec)
assert spec.loader
sys.modules[spec.name] = reader
spec.loader.exec_module(reader)


class RfidSpoolTests(unittest.TestCase):
    def test_maintenance_never_deletes_pending_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = reader.Spool(str(Path(tmp) / "spool.sqlite"))
            base = {
                "source_time": "2026-07-14T10:00:00",
                "source_sequence": 1,
                "connection_epoch": "11111111-1111-1111-1111-111111111111",
                "antenna": 1,
                "rssi": -45.0,
                "epc": "EPC",
                "tid": "TID",
                "time_quality": "HOST_CAPTURED",
            }
            sent = dict(base, client_uuid="22222222-2222-2222-2222-222222222222")
            pending = dict(base, client_uuid="33333333-3333-3333-3333-333333333333", source_sequence=2)
            spool.enqueue(sent)
            spool.enqueue(pending)
            spool.mark_sent(sent["client_uuid"])
            with spool.connect() as conn:
                conn.execute("UPDATE reads SET sent_at='2000-01-01T00:00:00' WHERE client_uuid=?", (sent["client_uuid"],))
                conn.execute("UPDATE reads SET created_at='2000-01-01T00:00:00' WHERE client_uuid=?", (pending["client_uuid"],))
                conn.commit()
            spool.maintenance()
            with spool.connect() as conn:
                rows = dict(conn.execute("SELECT client_uuid,state FROM reads").fetchall())
            self.assertNotIn(sent["client_uuid"], rows)
            self.assertEqual(rows.get(pending["client_uuid"]), "PENDING")


if __name__ == "__main__":
    unittest.main()
