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
            db.execute("""CREATE TABLE IF NOT EXISTS registry_refresh(
                stream TEXT PRIMARY KEY, position INTEGER NOT NULL DEFAULT 0,
                completed_at REAL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS registry_refresh_cycles(
                stream TEXT PRIMARY KEY, cycle INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS registry_refresh_seen(
                stream TEXT NOT NULL, source_id INTEGER NOT NULL, cycle INTEGER NOT NULL,
                PRIMARY KEY(stream,source_id))""")
            # Older releases did not persist which rows a partial refresh saw.
            # Restart such a scan so the first reconciled cycle cannot remove
            # a valid row that was visited before this schema existed.
            missing = db.execute("""SELECT r.stream FROM registry_refresh r
                LEFT JOIN registry_refresh_cycles c ON c.stream=r.stream
                WHERE c.stream IS NULL""").fetchall()
            for (stream,) in missing:
                db.execute("UPDATE registry_refresh SET position=0 WHERE stream=?", (stream,))
                db.execute("INSERT INTO registry_refresh_cycles VALUES(?,0)", (stream,))
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

    def refresh_position(self, stream):
        if stream not in {"warehouse", "tasks"}:
            raise ValueError("MutableRegistryRequired")
        with self.connect() as db:
            row = db.execute("SELECT position FROM registry_refresh WHERE stream=?", (stream,)).fetchone()
        return int(row[0]) if row else 0

    def commit_refresh(self, stream, rows, position, complete, high_watermark=None):
        if stream not in {"warehouse", "tasks"} or type(position) is not int or position < 0:
            raise ValueError("RegistryRefreshCursorInvalid")
        if high_watermark is not None and (type(high_watermark) is not int or high_watermark < 0):
            raise ValueError("RegistryRefreshHighWatermarkInvalid")
        now = time.time()
        prepared = []
        for source_id, source_at, payload in rows:
            if not isinstance(source_at, datetime):
                source_at = datetime.fromisoformat(str(source_at))
            prepared.append((stream, int(source_id), source_at.isoformat(),
                             canonical(metadata_only(payload)), now))
        with self.connect() as db:
            state = db.execute(
                "SELECT position FROM registry_refresh WHERE stream=?", (stream,)).fetchone()
            previous_position = int(state[0]) if state else 0
            cycle_row = db.execute(
                "SELECT cycle FROM registry_refresh_cycles WHERE stream=?", (stream,)).fetchone()
            cycle = int(cycle_row[0]) if cycle_row else 0
            if previous_position == 0:
                cycle += 1
                db.execute("DELETE FROM registry_refresh_seen WHERE stream=?", (stream,))
                db.execute("""INSERT INTO registry_refresh_cycles VALUES(?,?)
                    ON CONFLICT(stream) DO UPDATE SET cycle=excluded.cycle""", (stream, cycle))
            db.executemany("""INSERT INTO snapshots VALUES(?,?,?,?,?)
                ON CONFLICT(stream,source_id) DO UPDATE SET
                source_time=excluded.source_time,payload=excluded.payload,mirrored_at=excluded.mirrored_at""", prepared)
            db.executemany("""INSERT INTO registry_refresh_seen VALUES(?,?,?)
                ON CONFLICT(stream,source_id) DO UPDATE SET cycle=excluded.cycle""",
                ((stream, item[1], cycle) for item in prepared))
            if high_watermark is None:
                watermark = db.execute(
                    "SELECT position FROM watermarks WHERE stream=?", (stream,)).fetchone()
                high_watermark = int(json.loads(watermark[0])) if watermark else 0
            if complete:
                db.execute("""DELETE FROM snapshots
                    WHERE stream=? AND source_id<=? AND NOT EXISTS(
                        SELECT 1 FROM registry_refresh_seen seen
                        WHERE seen.stream=snapshots.stream
                        AND seen.source_id=snapshots.source_id AND seen.cycle=?)""",
                           (stream, high_watermark, cycle))
                db.execute("DELETE FROM registry_refresh_seen WHERE stream=?", (stream,))
            db.execute("""INSERT INTO registry_refresh VALUES(?,?,?)
                ON CONFLICT(stream) DO UPDATE SET position=excluded.position,
                completed_at=CASE WHEN excluded.completed_at IS NULL
                    THEN registry_refresh.completed_at ELSE excluded.completed_at END""",
                (stream, position, now if complete else None))
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
            refresh = db.execute("SELECT stream,position,completed_at FROM registry_refresh ORDER BY stream").fetchall()
        return {"retention_days": self.retention_days,
                "streams": {r[0]: {"synced_at": r[1], "caught_up": bool(r[2]), "records": r[3],
                                   "age_sec": max(0, time.time() - r[1])} for r in rows},
                "registry_refresh": {r[0]: {"position": r[1], "completed_at": r[2]} for r in refresh},
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
