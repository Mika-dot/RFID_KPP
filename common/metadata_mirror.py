"""Local, read-only metadata view of the last 93 days of primary SQL data.

Snapshots may be updated by synchronization. They are never SQL authority,
never lease storage and never a source of writes back into production tables.
The separate fallback delivery journal holds locally captured pending inputs.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from common.fallback_store import canonical, metadata_only


class MetadataMirror:
    def __init__(self, path, retention_days=93):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = max(93, int(retention_days))
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS snapshots(
                stream TEXT NOT NULL, source_id INTEGER NOT NULL, source_time TEXT NOT NULL,
                payload TEXT NOT NULL, mirrored_at REAL NOT NULL,
                PRIMARY KEY(stream,source_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS snapshots_time ON snapshots(stream,source_time,source_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS watermarks(
                stream TEXT PRIMARY KEY, position TEXT NOT NULL, synced_at REAL NOT NULL,
                caught_up INTEGER NOT NULL DEFAULT 0)""")
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def watermark(self, stream):
        with self.connect() as db:
            row = db.execute("SELECT position FROM watermarks WHERE stream=?", (stream,)).fetchone()
        return json.loads(row[0]) if row else None

    def commit_batch(self, stream, rows, position, caught_up=False):
        prepared = []
        now = time.time()
        for source_id, source_at, payload in rows:
            if not isinstance(source_at, datetime):
                source_at = datetime.fromisoformat(str(source_at))
            prepared.append((stream, int(source_id), source_at.isoformat(),
                             canonical(metadata_only(payload)), now))
        with self.connect() as db:
            db.executemany("""INSERT INTO snapshots VALUES(?,?,?,?,?)
                ON CONFLICT(stream,source_id) DO UPDATE SET
                source_time=excluded.source_time,payload=excluded.payload,mirrored_at=excluded.mirrored_at""", prepared)
            db.execute("""INSERT INTO watermarks VALUES(?,?,?,?)
                ON CONFLICT(stream) DO UPDATE SET position=excluded.position,
                synced_at=excluded.synced_at,caught_up=excluded.caught_up""",
                (stream, canonical(position), now, int(bool(caught_up))))
        return len(prepared)

    def recent(self, stream="events", limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("MirrorPageInvalid")
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM snapshots WHERE stream=? ORDER BY source_time DESC,source_id DESC LIMIT ?",
                              (stream, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def stats(self):
        with self.connect() as db:
            rows = db.execute("""SELECT w.stream,w.synced_at,w.caught_up,
                (SELECT COUNT(*) FROM snapshots s WHERE s.stream=w.stream)
                FROM watermarks w ORDER BY w.stream""").fetchall()
        return {"retention_days": self.retention_days,
                "streams": {r[0]: {"synced_at": r[1], "caught_up": bool(r[2]), "records": r[3],
                                   "age_sec": max(0, time.time() - r[1])} for r in rows},
                "bytes": sum(p.stat().st_size for p in (self.path, Path(str(self.path) + "-wal")) if p.exists())}

    def lookup(self, stream, source_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM snapshots WHERE stream=? AND source_id=?", (stream, source_id)).fetchone()
        return json.loads(row[0]) if row else None

    def counts_24h(self, now=None):
        cutoff = ((now or datetime.now()) - timedelta(hours=24)).isoformat()
        with self.connect() as db:
            row = db.execute("""SELECT
                SUM(CASE WHEN json_extract(payload,'$.RfidReadCount')>0 AND json_extract(payload,'$.IsReel')=1 AND json_extract(payload,'$.FinalDirection')='IN' THEN 1 ELSE 0 END),
                SUM(CASE WHEN json_extract(payload,'$.RfidReadCount')>0 AND json_extract(payload,'$.IsReel')=1 AND json_extract(payload,'$.FinalDirection')='OUT' THEN 1 ELSE 0 END),
                SUM(CASE WHEN json_extract(payload,'$.SessionCloseReason')='WAREHOUSE_ONLY' AND json_extract(payload,'$.IsReel')=1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN json_extract(payload,'$.NeedRecheck')=1 THEN 1 ELSE 0 END)
                FROM snapshots WHERE stream='events' AND source_time>=?""", (cutoff,)).fetchone()
        return dict(zip(("in_24h", "out_24h", "warehouse_only_24h", "recheck_24h"), [int(value or 0) for value in row]))

    def maintenance(self, now=None):
        cutoff = ((now or datetime.now()) - timedelta(days=self.retention_days)).isoformat()
        with self.connect() as db:
            result = db.execute("DELETE FROM snapshots WHERE source_time<?", (cutoff,))
            deleted = result.rowcount
        with self.connect() as db:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return deleted
