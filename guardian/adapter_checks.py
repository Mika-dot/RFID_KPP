"""Protected checks of production SQL writers and SKUD cursor, entirely in memory."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import sys
import tempfile
import types
import uuid
from datetime import datetime
from pathlib import Path


def check(root):
    sys.path.insert(0, str(root))
    statements, order = [], []
    class Conn:
        def __init__(self, fail=False):
            self.fail, self.state, self.committed, self.query = fail, 10, 10, ""
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def cursor(self): return self
        def execute(self, sql, *args):
            self.query = sql
            statements.append((sql, args))
            if sql.lstrip().startswith("MERGE"):
                self.state = int(args[1])
            return self
        def fetchone(self):
            if "SELECT StateValue" in self.query: return (self.state,)
            if "MAX([_id])" in self.query: return (25,)
            if "MAX(ExternalId2)" in self.query: return (0,)
            return None
        def fetchall(self): return []
        def commit(self):
            order.append("commit")
            if self.fail: raise RuntimeError("ModeledSqlOutage")
            self.committed = self.state
    db = Conn()
    odbc = types.SimpleNamespace(Connection=Conn, Cursor=Conn, connect=lambda *a, **k: db)
    sys.modules["pyodbc"] = odbc
    os.environ["PERIMETER_REPLICA_ENABLED"] = "0"
    def load(path, name):
        spec = importlib.util.spec_from_file_location(name, root/path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    rfid = load(Path("RFID_reader_v4/rfid_to_sql_v4.py"), "checked_rfid")
    rfid.Config.DB_CONN = "offline"
    at = "2026-10-07T08:00:00.123456"
    item = dict(client_uuid=str(uuid.uuid4()), source_time=at, source_sequence=17,
        connection_epoch="modeled-connection", antenna=2, rssi=-51.2, epc="A"*24, tid="B"*24, time_quality="SOURCE_TIME")
    with tempfile.TemporaryDirectory() as folder:
        spool = rfid.Spool(str(Path(folder)/"rfid.sqlite"))
        spool.enqueue(item)
        writer = rfid.SQLWriter(spool)
        original = spool.mark_sent
        def sent(key):
            if not order or order[-1] != "commit": raise RuntimeError("CandidateSpoolSentBeforeCommit")
            original(key)
            writer.running = False
        spool.mark_sent = sent
        writer.run()
        if spool.stats()[:2] != (0,1): raise RuntimeError("CandidateRfidDeliveryContract")
        args = next(args for sql,args in statements if "INSERT INTO dbo.RFID_Tags" in sql)
        if args[0] != item["client_uuid"] or args[9] != datetime.fromisoformat(at) or args[10] != 17 or args[5:7] != (item["epc"],item["tid"]):
            raise RuntimeError("CandidateRfidSourceFieldsChanged")
        failed_item = dict(item, client_uuid=str(uuid.uuid4()))
        spool.enqueue(failed_item)
        db.fail = True
        writer = rfid.SQLWriter(spool)
        failed = spool.mark_failed
        def fail(*args):
            failed(*args)
            writer.running = False
        spool.mark_failed = fail
        writer.run()
        if spool.stats()[0] != 1: raise RuntimeError("CandidateRfidOutageDropsPending")
        db.fail = False
    video = load(Path("RTSP/RTSP_yolo_DB_v3.py"), "checked_video")
    video.Config.DB_CONN = "offline"
    payload = dict(event_uuid=str(uuid.uuid4()), direction="0>1", from_camera=0, to_camera=1,
        captured_at=at, processed_at="2026-10-07T08:00:02", time_diff_sec=1.25,
        transport="FORKLIFT", reel_count=2, source_track_ids=[11,12])
    statements.clear()
    video.DBWriter.insert(payload, b"recorded-image-bytes")
    args = statements[-1][1]
    if (args[0] != payload["event_uuid"] or args[13] != datetime.fromisoformat(at)
            or args[7] != b"recorded-image-bytes" or args[16] != 2 or json.loads(args[17]) != [11,12]):
        raise RuntimeError("CandidateVideoSourceFieldsChanged")
    # SQL failure cannot mark a video event as SENT.
    with tempfile.TemporaryDirectory() as folder:
        spool = video.DurableEventSpool(str(Path(folder)/"video.sqlite"))
        with spool._connect() as local:
            local.execute("INSERT INTO events(event_uuid,payload_json,created_at) VALUES(?,?,?)",
                          (payload["event_uuid"], json.dumps(payload), at))
        writer = video.DBWriter(spool)
        db.fail = True
        original_failed = spool.mark_failed
        def failed_video(*args):
            original_failed(*args)
            writer.running = False
        spool.mark_failed = failed_video
        writer.run()
        if spool.stats()[0] != 1: raise RuntimeError("CandidateVideoOutageDropsPending")
        db.fail = False
    for prefix in ("SRC", "DST"):
        for name in ("SERVER", "DATABASE", "USERNAME", "PASSWORD"):
            os.environ[prefix+"_"+name] = "offline-placeholder"
    skud = load(Path("DB_RusGard/db_sync_v2.py"), "checked_skud")
    db.state = db.committed = 10
    count, cursor = skud.sync_page()
    if count != 0 or cursor != 25 or db.committed != 25:
        raise RuntimeError("CandidateSkudUnrelatedRowsBlockCursor")
    return 6


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    args = p.parse_args(argv)
    checks = check(args.root.resolve())
    print("OFFLINE_ADAPTER_CONTRACTS_OK", checks)


if __name__ == "__main__":
    main()
