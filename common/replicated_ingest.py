"""Durable peer copies of RFID/video inputs; never opens a reader or writes SQL.

An acknowledgement means SQLite WAL/FULL commit, not receipt in memory. Only a
business writer may mark SENT, after its fenced SQL commit. UUID and source time
are immutable across copies. Quorum covers acknowledged inputs, not observations
that were lost before enqueue/replication.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

MAX_BODY = 12 * 1024 * 1024
STREAMS = {"rfid", "video"}
RFID_FIELDS = ("client_uuid", "source_time", "source_sequence", "connection_epoch",
               "antenna", "rssi", "epc", "tid", "time_quality")
VIDEO_FIELDS = ("event_uuid", "direction", "from_camera", "to_camera", "captured_at",
                "processed_at", "time_diff_sec", "transport", "reel_count", "source_track_ids")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def envelope(stream, payload, image=None):
    if stream not in STREAMS or not isinstance(payload, dict):
        raise ValueError("InvalidReplicaStream")
    fields = RFID_FIELDS if stream == "rfid" else VIDEO_FIELDS
    if set(payload) != set(fields):
        raise ValueError("InvalidReplicaPayload")
    key = payload[fields[0]]
    if str(uuid.UUID(str(key))) != key:
        raise ValueError("InvalidReplicaUuid")
    if stream == "rfid":
        datetime.fromisoformat(payload["source_time"])
        if (type(payload["source_sequence"]) is not int or payload["source_sequence"] < 0
                or type(payload["antenna"]) is not int or not math.isfinite(float(payload["rssi"]))
                or any(not isinstance(payload[k], str) for k in ("epc", "tid", "connection_epoch", "time_quality"))
                or image is not None):
            raise ValueError("InvalidReplicaRfid")
    else:
        datetime.fromisoformat(payload["captured_at"])
        datetime.fromisoformat(payload["processed_at"])
        if (type(payload["reel_count"]) is not int or payload["reel_count"] < 1
                or not math.isfinite(float(payload["time_diff_sec"]))
                or not isinstance(payload["source_track_ids"], list)):
            raise ValueError("InvalidReplicaVideo")
    data = {"version": 1, "stream": stream, "uuid": key, "payload": payload,
            "image": base64.b64encode(image).decode("ascii") if image is not None else None}
    raw = canonical(data).encode("utf-8")
    if len(raw) > MAX_BODY - 1024:
        raise ValueError("ReplicaPayloadTooLarge")
    return dict(data, digest=hashlib.sha256(raw).hexdigest())


def validate_record(record):
    if not isinstance(record, dict) or set(record) != {"version", "stream", "uuid", "payload", "image", "digest"}:
        raise ValueError("InvalidReplicaEnvelope")
    if type(record["version"]) is not int or record["version"] != 1:
        raise ValueError("InvalidReplicaVersion")
    image = base64.b64decode(record["image"], validate=True) if record["image"] is not None else None
    checked = envelope(record["stream"], record["payload"], image)
    if checked != record:
        raise ValueError("ReplicaChecksumMismatch")
    return image


def validate_peers(nodes, node_id=None):
    if len(nodes) != 3 or len({n["id"] for n in nodes}) != 3:
        raise ValueError("ReplicaRequiresThreeNodes")
    if node_id is not None and node_id not in {n["id"] for n in nodes}:
        raise ValueError("ReplicaUnknownNode")
    for n in nodes:
        u = urlsplit(n["url"])
        if (u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password
                or u.query or u.fragment or u.path not in {"", "/"}
                or not isinstance(n["id"], str) or not n["id"].replace("-", "").replace("_", "").isalnum()):
            raise ValueError("ReplicaPeerUrlInvalid")
    return nodes


class ReplicaJournal:
    def __init__(self, path, node_id):
        self.path, self.node_id = Path(path), node_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS replica(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL, uuid TEXT NOT NULL,
                digest TEXT NOT NULL, record TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
                created REAL NOT NULL, sent REAL, UNIQUE(stream,uuid))""")
            db.execute("""CREATE TABLE IF NOT EXISTS replica_ack(
                stream TEXT NOT NULL, uuid TEXT NOT NULL, node TEXT NOT NULL,
                PRIMARY KEY(stream,uuid,node))""")
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def put(self, record):
        validate_record(record)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO replica(stream,uuid,digest,record,created) VALUES(?,?,?,?,?)",
                       (record["stream"], record["uuid"], record["digest"], canonical(record), time.time()))
            found = db.execute("SELECT digest FROM replica WHERE stream=? AND uuid=?",
                               (record["stream"], record["uuid"])).fetchone()
            if found[0] != record["digest"]:
                raise ValueError("ReplicaUuidContentConflict")
            db.execute("INSERT OR IGNORE INTO replica_ack VALUES(?,?,?)",
                       (record["stream"], record["uuid"], self.node_id))
        return {"durable": True, "node": self.node_id, "uuid": record["uuid"], "digest": record["digest"]}

    def ack(self, record, node):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO replica_ack VALUES(?,?,?)", (record["stream"], record["uuid"], node))

    def ack_count(self, record):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM replica_ack WHERE stream=? AND uuid=?",
                              (record["stream"], record["uuid"])).fetchone()[0]

    def sent(self, stream, key, digest):
        with self.connect() as db:
            found = db.execute("SELECT digest FROM replica WHERE stream=? AND uuid=?", (stream, key)).fetchone()
            if not found or found[0] != digest:
                raise ValueError("ReplicaCommitUnknown")
            db.execute("UPDATE replica SET state='SENT',sent=COALESCE(sent,?) WHERE stream=? AND uuid=?",
                       (time.time(), stream, key))

    def get(self, stream, key):
        with self.connect() as db:
            row = db.execute("SELECT record FROM replica WHERE stream=? AND uuid=?", (stream, key)).fetchone()
        return json.loads(row[0]) if row else None

    def page(self, after=0, limit=100, pending=False):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("ReplicaPageInvalid")
        where = " AND state='PENDING'" if pending else ""
        with self.connect() as db:
            rows = db.execute("SELECT seq,record,state FROM replica WHERE seq>?" + where + " ORDER BY seq LIMIT ?",
                              (after, limit)).fetchall()
        result, size = [], 0
        for seq, raw, state in rows:
            size += len(raw.encode("utf-8")) + 100
            if result and size > MAX_BODY - 1024:
                break
            result.append({"cursor": seq, "record": json.loads(raw), "state": state})
        return {"items": result, "cursor": result[-1]["cursor"] if result else after}

    def stats(self):
        with self.connect() as db:
            total, pending, oldest = db.execute("SELECT COUNT(*),SUM(state='PENDING'),MIN(CASE WHEN state='PENDING' THEN created END) FROM replica").fetchone()
            protected = db.execute("""SELECT COUNT(*) FROM replica r WHERE state='PENDING' AND
                (SELECT COUNT(*) FROM replica_ack a WHERE a.stream=r.stream AND a.uuid=r.uuid)>=2""").fetchone()[0]
        return {"records": total, "pending": pending or 0, "quorum_pending": protected,
                "oldest_pending_sec": max(0, time.time()-oldest) if oldest else 0,
                "bytes": sum(p.stat().st_size for p in (self.path, Path(str(self.path)+"-wal")) if p.exists())}

    def maintenance(self, retention_days=14):
        with self.connect() as db:
            db.execute("DELETE FROM replica_ack WHERE EXISTS(SELECT 1 FROM replica r WHERE r.stream=replica_ack.stream AND r.uuid=replica_ack.uuid AND r.state='SENT' AND r.sent<?)",
                       (time.time()-max(1, retention_days)*86400,))
            db.execute("DELETE FROM replica WHERE state='SENT' AND sent<?", (time.time()-max(1, retention_days)*86400,))


class ReplicatedDelivery:
    def __init__(self, journal=None, nodes=(), request=None, fallback=None):
        self.journal, self.nodes, self.fallback = journal, list(nodes), fallback
        self.request = request or self.http
        self.peer_retry = {}
        self.last_fallback_maintenance = 0

    @classmethod
    def from_env(cls):
        fallback = None
        if os.getenv("PERIMETER_FALLBACK_ENABLED", "0") == "1":
            from common.fallback_store import FallbackStore
            state_dir = Path(os.environ.get("PERIMETER_HA_STATE_DIR", "."))
            fallback = FallbackStore(
                os.environ.get("PERIMETER_FALLBACK_PATH", str(state_dir / "fallback.sqlite")),
                int(os.getenv("PERIMETER_FALLBACK_RETENTION_DAYS", "93")),
            )
        if os.getenv("PERIMETER_REPLICA_ENABLED", "0") != "1":
            return cls(fallback=fallback)
        node_id = os.environ["PERIMETER_HA_NODE"]
        nodes = validate_peers(json.loads(os.environ["PERIMETER_REPLICA_NODES"]), node_id)
        return cls(ReplicaJournal(Path(os.environ["PERIMETER_HA_STATE_DIR"])/"replica.sqlite", node_id), nodes, fallback=fallback)

    @staticmethod
    def http(url, body=None):
        # No redirects: an authenticated replica request must stay at its peer.
        from guardian.net import json_request
        return json_request(url, os.environ["PERIMETER_HA_TOKEN"], body=body, timeout=2, max_bytes=MAX_BODY)

    def ensure(self, stream, payload, image=None):
        record = envelope(stream, payload, image) if self.journal is not None or self.fallback is not None else None
        if self.fallback is not None:
            self.fallback.put(stream, record["uuid"], payload)
        if self.journal is None:
            return record
        self.journal.put(record)
        if self.journal.ack_count(record) >= 2:
            return record
        for peer in self.nodes:
            if peer["id"] == self.journal.node_id:
                continue
            if time.monotonic() < self.peer_retry.get(peer["id"], 0):
                continue
            try:
                code, ack = self.request(peer["url"].rstrip("/")+"/replica/put", record)
                if (code == 200 and ack == {"durable": True, "node": peer["id"], "uuid": record["uuid"], "digest": record["digest"]}):
                    self.journal.ack(record, peer["id"])
                    self.peer_retry.pop(peer["id"], None)
                    if self.journal.ack_count(record) >= 2:
                        break
                else:
                    self.peer_retry[peer["id"]] = time.monotonic()+15
            except Exception:
                self.peer_retry[peer["id"]] = time.monotonic()+15
                continue
        if self.journal.ack_count(record) < 2:
            raise RuntimeError("ReplicaQuorumUnavailable")
        return record

    def committed(self, record):
        if record is None:
            return
        if self.journal is not None:
            self.journal.sent(record["stream"], record["uuid"], record["digest"])
        if self.fallback is not None:
            saved = self.fallback.put(record["stream"], record["uuid"], record["payload"])
            self.fallback.mark_committed(record["stream"], record["uuid"], saved["digest"])
            if time.monotonic() - self.last_fallback_maintenance >= 60:
                self.fallback.maintenance()
                self.last_fallback_maintenance = time.monotonic()
        if self.journal is None:
            return
        body = {k: record[k] for k in ("stream", "uuid", "digest")}
        for peer in self.nodes:
            if peer["id"] != self.journal.node_id:
                try:
                    self.request(peer["url"].rstrip("/")+"/replica/commit", body)
                except Exception:
                    pass  # Replay is idempotent when an acknowledgement is lost.


def replay_local(journal, paths):
    """Import peer PENDING into ordinary spools; existing SQL writers deliver them.

    Caller must hold a valid executor lease. Import itself is local/idempotent;
    business SQL fencing independently checks the writer's epoch at commit.
    """
    for item in journal.page(limit=100, pending=True)["items"]:
        record = item["record"]
        path = Path(paths[record["stream"]])
        if not path.is_file():
            continue  # Only the normal spool constructor creates its schema.
        image = validate_record(record)
        p = record["payload"]
        with closing(sqlite3.connect(path, timeout=2)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            if record["stream"] == "rfid":
                db.execute("INSERT OR IGNORE INTO reads(client_uuid,source_time,source_sequence,connection_epoch,antenna,rssi,epc,tid,time_quality,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           tuple(p[k] for k in RFID_FIELDS)+(datetime.now().isoformat(),))
                found = db.execute("SELECT "+",".join(RFID_FIELDS)+",state FROM reads WHERE client_uuid=?", (record["uuid"],)).fetchone()
                existing = dict(zip(RFID_FIELDS, found[:-1]))
                existing["tid"] = existing["tid"] or ""
                if existing != p:
                    raise ValueError("LocalReplicaUuidConflict")
                row = (found[-1],)
            else:
                db.execute("INSERT OR IGNORE INTO events(event_uuid,payload_json,image_blob,created_at) VALUES(?,?,?,?)",
                           (record["uuid"], canonical(p), image, datetime.now().isoformat()))
                found = db.execute("SELECT payload_json,image_blob,state FROM events WHERE event_uuid=?", (record["uuid"],)).fetchone()
                if json.loads(found[0]) != p or found[1] != image:
                    raise ValueError("LocalReplicaUuidConflict")
                row = (found[-1],)
        if row and row[0] == "SENT":
            journal.sent(record["stream"], record["uuid"], record["digest"])
