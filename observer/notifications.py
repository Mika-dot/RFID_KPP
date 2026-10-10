"""Durable, deduplicated incident transitions to an operator-configured webhook.

No recipients/actions are created. Delivery is disabled until a URL is configured.
Only classifications and timestamps leave the observer, never raw source rows.
"""
from __future__ import annotations
import json
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from guardian.net import json_request
from contextlib import contextmanager


def webhook_request(*args, **kwargs):
    return json_request(*args, **kwargs, parse_json=False)


class AlertOutbox:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS incident(key TEXT PRIMARY KEY,status TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,body TEXT,sent INTEGER DEFAULT 0,attempts INTEGER DEFAULT 0,next REAL DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS candidate(key TEXT PRIMARY KEY,status TEXT,samples INTEGER)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def transition(self, key, status, at, persistence=1):
        with self.connect() as db:
            old = db.execute("SELECT status FROM incident WHERE key=?", (key,)).fetchone()
            if old and old[0] == status:
                db.execute("DELETE FROM candidate WHERE key=?",(key,))
                return
            if status in {"warning","critical","collector_error"} and persistence>1:
                pending=db.execute("SELECT status,samples FROM candidate WHERE key=?",(key,)).fetchone()
                samples=pending[1]+1 if pending and pending[0]==status else 1
                db.execute("INSERT OR REPLACE INTO candidate VALUES(?,?,?)",(key,status,samples))
                if samples<persistence:return
            db.execute("DELETE FROM candidate WHERE key=?",(key,))
            db.execute("INSERT OR REPLACE INTO incident VALUES(?,?)", (key, status))
            if status in {"warning", "critical", "collector_error"} or (old and old[0] in {"warning", "critical", "collector_error"}):
                event_id = str(uuid.uuid4())
                body = {"id": event_id, "incident": key, "status": status, "previous": old[0] if old else None, "at": at}
                db.execute("INSERT INTO outbox(id,body) VALUES(?,?)", (event_id, json.dumps(body)))

    def deliver(self, url=None, token=None, request=webhook_request):
        if not url:
            return 0
        target = urlsplit(url)
        if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password or target.fragment:
            raise ValueError("InvalidNotificationEndpoint")
        sent = 0
        with self.connect() as db:
            rows = db.execute("SELECT id,body,attempts FROM outbox WHERE sent=0 AND next<=? ORDER BY rowid LIMIT 10", (time.time(),)).fetchall()
        for key, body, attempts in rows:
            try:
                code, _ = request(url, token, body=json.loads(body), timeout=3)
                ok = 200 <= code < 300
            except Exception:
                ok = False
            with self.connect() as db:
                db.execute("UPDATE outbox SET sent=?,attempts=attempts+1,next=? WHERE id=?", (int(ok), time.time()+min(3600, 5*2**min(attempts, 9)), key))
                db.execute("DELETE FROM outbox WHERE sent=1 AND rowid<(SELECT COALESCE(MAX(rowid),0)-1000 FROM outbox)")
            sent += int(ok)
        return sent
