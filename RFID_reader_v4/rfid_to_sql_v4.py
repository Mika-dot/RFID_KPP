#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RFID reader -> durable SQLite spool -> SQL Server v4.

Каждое чтение сначала фиксируется локально (WAL+FULL), затем отдельный writer
доставляет его в SQL с ClientReadUuid. Ошибка/rollback не может потерять начало
пакета. Исходное время чтения не заменяется временем повторной отправки.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from common.single_instance import SingleInstanceLock  # noqa: E402

logging.basicConfig(
    level=getattr(logging, os.getenv("RFID_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rfid-reader-v4")


class Config:
    DLL_PATH = os.getenv("RFID_DLL_PATH", "")
    READER_IP = os.getenv("RFID_READER_IP", "")
    READER_PORT = int(os.getenv("RFID_READER_PORT", "8888"))
    DB_CONN = os.getenv("RFID_DB_CONNECTION", "")
    SPOOL_PATH = os.getenv("RFID_SPOOL_PATH", "rfid_spool_v4.sqlite")
    RECONNECT_SEC = float(os.getenv("RFID_RECONNECT_DELAY_SEC", "5"))
    READ_SLEEP_SEC = float(os.getenv("RFID_READ_SLEEP_SEC", "0.01"))
    IDLE_SLEEP_SEC = float(os.getenv("RFID_IDLE_SLEEP_SEC", "0.05"))
    RETRY_BASE_SEC = float(os.getenv("RFID_DB_RETRY_BASE_SEC", "2"))
    RETRY_MAX_SEC = float(os.getenv("RFID_DB_RETRY_MAX_SEC", "300"))
    CONSOLE = os.getenv("RFID_CONSOLE_OUTPUT", "1") == "1"
    RECONNECT_UNCERTAIN_SEC = float(os.getenv("RFID_RECONNECT_UNCERTAIN_SEC", "60"))
    LOCK_PATH = os.getenv("RFID_READER_LOCK_FILE", SPOOL_PATH + ".lock")
    SPOOL_RETENTION_DAYS = int(os.getenv("RFID_SPOOL_RETENTION_DAYS", "14"))
    SPOOL_WARN_MB = int(os.getenv("RFID_SPOOL_WARN_MB", "2048"))
    HEARTBEAT_SEC = float(os.getenv("RFID_HEARTBEAT_SEC", "10"))


def validate() -> None:
    missing = [name for name, value in {
        "RFID_DLL_PATH": Config.DLL_PATH,
        "RFID_READER_IP": Config.READER_IP,
        "RFID_DB_CONNECTION": Config.DB_CONN,
    }.items() if not value]
    if missing:
        raise RuntimeError("Не заданы: " + ", ".join(missing))
    if not Path(Config.DLL_PATH).exists():
        raise FileNotFoundError(Config.DLL_PATH)


def parse_buf(buf, length: int) -> Optional[Dict[str, object]]:
    if length < 8:
        return None
    try:
        data = [b & 0xFF for b in buf[:length]]
        uii_len = data[0]
        if uii_len < 3 or uii_len + 1 >= length:
            return None
        tid_len = data[uii_len + 1]
        tid_start = uii_len + 2
        tid_len = min(tid_len, max(0, length - tid_start))
        rssi_idx = tid_start + tid_len
        if rssi_idx + 3 > length:
            return None
        ant_idx = rssi_idx + 2
        epc_start = 3
        epc_end = epc_start + ((uii_len * 2 - 4) // 2)
        epc = "".join(f"{b:02X}" for b in data[epc_start:epc_end])
        tid = "".join(f"{b:02X}" for b in data[tid_start:tid_start + tid_len]) if tid_len else ""
        rssi_raw = (data[rssi_idx] << 8) | data[rssi_idx + 1]
        return {
            "epc": epc,
            "tid": tid,
            "rssi": (rssi_raw - 65535) / 10.0,
            "antenna": data[ant_idx],
        }
    except Exception:
        log.exception("Ошибка разбора RFID buffer")
        return None


class Spool:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with self.connect() as conn:
            conn.execute(
                """
CREATE TABLE IF NOT EXISTS reads(
  client_uuid TEXT PRIMARY KEY,
  source_time TEXT NOT NULL,
  source_sequence INTEGER NOT NULL,
  connection_epoch TEXT NOT NULL,
  antenna INTEGER NOT NULL,
  rssi REAL NOT NULL,
  epc TEXT NOT NULL,
  tid TEXT,
  time_quality TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'PENDING',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt REAL NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT
)
"""
            )
        self.maintenance()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def enqueue(self, item: Dict[str, object]) -> None:
        with self.lock, self.connect() as conn:
            conn.execute(
                """
INSERT INTO reads(client_uuid,source_time,source_sequence,connection_epoch,antenna,rssi,epc,tid,time_quality,created_at)
VALUES(?,?,?,?,?,?,?,?,?,?)
""",
                (
                    item["client_uuid"], item["source_time"], item["source_sequence"], item["connection_epoch"],
                    item["antenna"], item["rssi"], item["epc"], item["tid"], item["time_quality"], datetime.now().isoformat(),
                ),
            )
            conn.commit()

    def maintenance(self) -> None:
        cutoff = (datetime.now() - timedelta(days=max(1, Config.SPOOL_RETENTION_DAYS))).isoformat()
        try:
            with self.lock, self.connect() as conn:
                conn.execute("DELETE FROM reads WHERE state='SENT' AND sent_at IS NOT NULL AND sent_at<?", (cutoff,))
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            size_mb = self.path.stat().st_size / (1024 * 1024) if self.path.exists() else 0.0
            if size_mb >= Config.SPOOL_WARN_MB:
                log.critical("RFID spool %.1f MB превышает порог %s MB; данные не удаляются до доставки", size_mb, Config.SPOOL_WARN_MB)
        except Exception:
            log.exception("RFID spool maintenance failed")

    def next_pending(self) -> Optional[Tuple]:
        with self.lock, self.connect() as conn:
            return conn.execute(
                """
SELECT client_uuid,source_time,source_sequence,connection_epoch,antenna,rssi,epc,tid,time_quality,attempts
FROM reads WHERE state='PENDING' AND next_attempt<=? ORDER BY created_at LIMIT 1
""",
                (time.time(),),
            ).fetchone()

    def mark_sent(self, client_uuid: str) -> None:
        with self.lock, self.connect() as conn:
            conn.execute("UPDATE reads SET state='SENT',sent_at=?,last_error=NULL WHERE client_uuid=?", (datetime.now().isoformat(), client_uuid))
            conn.commit()

    def mark_failed(self, client_uuid: str, attempts: int, error: str) -> None:
        delay = min(Config.RETRY_MAX_SEC, Config.RETRY_BASE_SEC * (2 ** min(attempts, 8)))
        with self.lock, self.connect() as conn:
            conn.execute(
                "UPDATE reads SET attempts=?,next_attempt=?,last_error=? WHERE client_uuid=?",
                (attempts + 1, time.time() + delay, error[:1000], client_uuid),
            )
            conn.commit()

    def stats(self) -> Tuple[int, int, int]:
        with self.lock, self.connect() as conn:
            row = conn.execute(
                "SELECT SUM(CASE WHEN state='PENDING' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN state='SENT' THEN 1 ELSE 0 END), COUNT(*) FROM reads"
            ).fetchone()
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


class SQLWriter(threading.Thread):
    def __init__(self, spool: Spool) -> None:
        super().__init__(name="rfid-sql-writer", daemon=True)
        self.spool = spool
        self.running = True
        self.sent_since_maintenance = 0
        self.delivered_total = 0
        self.failed_total = 0
        self.last_success_at: Optional[datetime] = None
        self.last_error: str = ""

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        import pyodbc
        while self.running:
            row = self.spool.next_pending()
            if row is None:
                time.sleep(0.2)
                continue
            client_uuid, source_time, sequence, epoch, antenna, rssi, epc, tid, quality, attempts = row
            try:
                batch_uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, epoch))
                source_dt = datetime.fromisoformat(source_time)
                with pyodbc.connect(Config.DB_CONN, autocommit=False, timeout=10) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
IF NOT EXISTS(SELECT 1 FROM dbo.RFID_Tags WHERE ClientReadUuid=?)
BEGIN
 INSERT INTO dbo.RFID_Tags(
   RecordTime,TagTime,Antenna,RSSI,EPC,TID,
   ClientReadUuid,ReceivedAt,SourceReaderTime,SourceSequence,IngestBatchId,TimeQuality
 ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
END
""",
                        client_uuid,
                        source_dt,
                        source_dt.strftime("%H:%M:%S.%f")[:-3],
                        int(antenna),
                        round(float(rssi), 1),
                        epc,
                        tid or None,
                        client_uuid,
                        datetime.now(),
                        source_dt,
                        int(sequence),
                        batch_uuid,
                        quality,
                    )
                    conn.commit()
                self.spool.mark_sent(client_uuid)
                self.delivered_total += 1
                self.last_success_at = datetime.now()
                self.last_error = ""
                self.sent_since_maintenance += 1
                if self.sent_since_maintenance >= 1000:
                    self.spool.maintenance()
                    self.sent_since_maintenance = 0
            except Exception as exc:
                self.failed_total += 1
                self.last_error = str(exc)
                log.warning("RFID DB retry %s: %s", int(attempts) + 1, exc)
                self.spool.mark_failed(client_uuid, int(attempts), str(exc))


def load_library():
    lib = ctypes.CDLL(Config.DLL_PATH)
    lib.TCPConnect.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    lib.TCPConnect.restype = ctypes.c_int
    lib.TCPDisconnect.argtypes = []
    lib.TCPDisconnect.restype = None
    lib.UHFInventory.argtypes = []
    lib.UHFInventory.restype = ctypes.c_int
    lib.UHFStopGet.argtypes = []
    lib.UHFStopGet.restype = ctypes.c_int
    lib.UHF_GetReceived_EX.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ubyte * 512)]
    lib.UHF_GetReceived_EX.restype = ctypes.c_int
    return lib


def disconnect(lib) -> None:
    try:
        lib.UHFStopGet()
    except Exception:
        pass
    try:
        lib.TCPDisconnect()
    except Exception:
        pass


def enqueue_with_backpressure(spool: Spool, item: Dict[str, object]) -> None:
    delay = 0.25
    while True:
        try:
            spool.enqueue(item)
            return
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.critical("RFID local spool unavailable; чтение удерживается в памяти: %s", exc)
            time.sleep(delay)
            delay = min(5.0, delay * 2)


def main() -> int:
    instance_lock = SingleInstanceLock(Config.LOCK_PATH)
    validate()
    lib = load_library()
    spool = Spool(Config.SPOOL_PATH)
    writer = SQLWriter(spool)
    writer.start()
    sequence = 0
    reads_total = 0
    reads_since_heartbeat = 0
    last_read_at: Optional[datetime] = None
    last_heartbeat = time.monotonic()
    try:
        while True:
            epoch = str(uuid.uuid4())
            reconnect_at = datetime.now()
            rc = lib.TCPConnect(Config.READER_IP.encode("utf-8"), Config.READER_PORT)
            if rc != 0:
                log.error("TCPConnect code=%s", rc)
                time.sleep(Config.RECONNECT_SEC)
                continue
            lib.UHFInventory()
            log.info("RFID reader connected, epoch=%s", epoch)
            try:
                while True:
                    length = ctypes.c_int(0)
                    buf = (ctypes.c_ubyte * 512)()
                    result = lib.UHF_GetReceived_EX(ctypes.byref(length), ctypes.byref(buf))
                    if result == 0 and length.value > 0:
                        parsed = parse_buf(buf, length.value)
                        if parsed:
                            sequence += 1
                            reads_total += 1
                            reads_since_heartbeat += 1
                            source_time = datetime.now()
                            last_read_at = source_time
                            # После reconnect контроллер мог выдать буфер. Время тогда приблизительное,
                            # но чтение не теряется и явно помечено для downstream.
                            quality = "APPROXIMATE_AFTER_RECONNECT" if (source_time - reconnect_at).total_seconds() < Config.RECONNECT_UNCERTAIN_SEC else "HOST_CAPTURE_TIME"
                            item = {
                                "client_uuid": str(uuid.uuid4()),
                                "source_time": source_time.isoformat(),
                                "source_sequence": sequence,
                                "connection_epoch": epoch,
                                "antenna": int(parsed["antenna"]),
                                "rssi": float(parsed["rssi"]),
                                "epc": str(parsed["epc"]),
                                "tid": str(parsed["tid"]),
                                "time_quality": quality,
                            }
                            enqueue_with_backpressure(spool, item)
                            if Config.CONSOLE:
                                print(f"{source_time:%H:%M:%S.%f}"[:-3], parsed["antenna"], f"{parsed['rssi']:.1f}", parsed["epc"], "✓ spool", flush=True)
                    else:
                        time.sleep(Config.IDLE_SLEEP_SEC)
                    now_mono = time.monotonic()
                    if now_mono - last_heartbeat >= Config.HEARTBEAT_SEC:
                        pending, sent, total = spool.stats()
                        age = "нет чтений" if last_read_at is None else f"{(datetime.now()-last_read_at).total_seconds():.1f}с назад"
                        db_age = "никогда" if writer.last_success_at is None else f"{(datetime.now()-writer.last_success_at).total_seconds():.1f}с назад"
                        log.info(
                            "STATUS reader=CONNECTED reads_total=%s reads_%ss=%s last_read=%s spool_pending=%s spool_sent=%s spool_total=%s db_delivered=%s db_last_ok=%s db_errors=%s",
                            reads_total, int(Config.HEARTBEAT_SEC), reads_since_heartbeat, age, pending, sent, total,
                            writer.delivered_total, db_age, writer.failed_total,
                        )
                        reads_since_heartbeat = 0
                        last_heartbeat = now_mono
                    time.sleep(Config.READ_SLEEP_SEC)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("RFID connection epoch failed")
            finally:
                disconnect(lib)
                time.sleep(Config.RECONNECT_SEC)
    except KeyboardInterrupt:
        log.info("Остановка")
    finally:
        writer.stop()
        writer.join(timeout=5)
        disconnect(lib)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
