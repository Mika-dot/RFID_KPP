"""Metadata-only local fallback journal for extended SQL outages.

The normal RFID/video spools remain the delivery source of truth.  This small
SQLite journal is an additional three-month safety copy: it stores identity,
source time and correlation metadata, deliberately strips images and other
large binary fields, and never contains SQL credentials.  A committed row is
retained until the configured retention period so an operator can audit or
replay it after the primary database returns.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


DEFAULT_RETENTION_DAYS = 93
_DROP_WORDS = ("image", "photo", "snapshot", "frame", "jpeg", "jpg", "png", "thumbnail", "blob")


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def metadata_only(value: object, key: str = "") -> object:
    """Return JSON-safe metadata with image/binary payloads removed."""
    lowered = key.lower()
    if any(word in lowered for word in _DROP_WORDS):
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if isinstance(value, Mapping):
        result = {}
        for child_key, child_value in value.items():
            cleaned = metadata_only(child_value, str(child_key))
            if cleaned is not None:
                result[str(child_key)] = cleaned
        return result
    if isinstance(value, (list, tuple)):
        return [cleaned for item in value if (cleaned := metadata_only(item, key)) is not None]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class FallbackStore:
    """Durable bounded metadata journal with idempotent UUID writes."""

    def __init__(self, path: str | os.PathLike[str], retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = max(DEFAULT_RETENTION_DAYS, int(retention_days))
        with self.connect() as db:
            db.execute(
                """
CREATE TABLE IF NOT EXISTS fallback_records(
    stream TEXT NOT NULL,
    record_uuid TEXT NOT NULL,
    source_time TEXT,
    payload TEXT NOT NULL,
    digest TEXT NOT NULL,
    created REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    committed_at REAL,
    PRIMARY KEY(stream,record_uuid)
)
"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS fallback_state_created ON fallback_records(state,created)")
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

    def put(self, stream: str, record_uuid: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(stream, str) or not stream or not isinstance(record_uuid, str) or not record_uuid:
            raise ValueError("FallbackIdentityInvalid")
        cleaned = metadata_only(dict(payload))
        if not isinstance(cleaned, dict):
            raise ValueError("FallbackPayloadInvalid")
        raw = canonical(cleaned)
        digest = hashlib.sha256((stream + "|" + record_uuid + "|" + raw).encode("utf-8")).hexdigest()
        source_time = cleaned.get("source_time") or cleaned.get("captured_at") or cleaned.get("processed_at")
        if source_time is not None:
            try:
                datetime.fromisoformat(str(source_time))
            except ValueError:
                raise ValueError("FallbackSourceTimeInvalid") from None
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO fallback_records(stream,record_uuid,source_time,payload,digest,created) VALUES(?,?,?,?,?,?)",
                (stream, record_uuid, str(source_time) if source_time is not None else None, raw, digest, now),
            )
            row = db.execute(
                "SELECT digest,state FROM fallback_records WHERE stream=? AND record_uuid=?",
                (stream, record_uuid),
            ).fetchone()
            if row is None or row[0] != digest:
                raise ValueError("FallbackUuidContentConflict")
        return {"stream": stream, "uuid": record_uuid, "digest": digest, "state": row[1]}

    def mark_committed(self, stream: str, record_uuid: str, digest: str) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT digest FROM fallback_records WHERE stream=? AND record_uuid=?",
                (stream, record_uuid),
            ).fetchone()
            if row is None or row[0] != digest:
                raise ValueError("FallbackCommitUnknown")
            db.execute(
                "UPDATE fallback_records SET state='COMMITTED',committed_at=COALESCE(committed_at,?) WHERE stream=? AND record_uuid=?",
                (time.time(), stream, record_uuid),
            )

    def page(self, limit: int = 100, pending: bool = True) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("FallbackPageInvalid")
        where = " WHERE state='PENDING'" if pending else ""
        with self.connect() as db:
            rows = db.execute(
                "SELECT stream,record_uuid,source_time,payload,digest,created,state FROM fallback_records"
                + where + " ORDER BY created,stream,record_uuid LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "stream": row[0],
                "uuid": row[1],
                "source_time": row[2],
                "payload": json.loads(row[3]),
                "digest": row[4],
                "created": row[5],
                "state": row[6],
            }
            for row in rows
        ]

    def replay(self, callback: Callable[[dict[str, Any]], None], limit: int = 100) -> int:
        count = 0
        for record in self.page(limit=limit, pending=True):
            callback(record)
            self.mark_committed(record["stream"], record["uuid"], record["digest"])
            count += 1
        return count

    def stats(self) -> dict[str, Any]:
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM fallback_records").fetchone()[0]
            pending, oldest = db.execute("SELECT COUNT(*),MIN(created) FROM fallback_records WHERE state='PENDING'").fetchone()
        files = [self.path, Path(str(self.path) + "-wal")]
        return {
            "records": int(total or 0),
            "pending": int(pending or 0),
            "retention_days": self.retention_days,
            "oldest_pending_sec": max(0.0, time.time() - oldest) if oldest else 0.0,
            "bytes": sum(path.stat().st_size for path in files if path.exists()),
        }

    def maintenance(self) -> int:
        cutoff = time.time() - self.retention_days * 86400
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM fallback_records WHERE state='COMMITTED' AND created<?",
                (cutoff,),
            )
            deleted = int(cursor.rowcount or 0)
        with self.connect() as db:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return deleted
