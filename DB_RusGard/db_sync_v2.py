#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Надёжная синхронизация RusGuard -> SQL v2.

Читает по монотонному ExternalId2, страницами, без ограничения «последние сутки».
Курсор обновляется только в той же транзакции, где зафиксированы вставки.
Время CreatedAt остаётся временем физического события; ReceivedAt — доставка.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import pyodbc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from common.single_instance import SingleInstanceLock  # noqa: E402

logging.basicConfig(
    level=getattr(logging, os.getenv("RUSGUARD_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("rusguard-sync-v2")


def require(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise RuntimeError(f"Не задана переменная {name}")
    return value


def conn_str(prefix: str) -> str:
    return (
        f"DRIVER={{{os.getenv(prefix + '_DRIVER', 'ODBC Driver 18 for SQL Server')}}};"
        f"SERVER={require(prefix + '_SERVER')};DATABASE={require(prefix + '_DATABASE')};"
        f"UID={require(prefix + '_USERNAME')};PWD={require(prefix + '_PASSWORD')};"
        "Encrypt=yes;TrustServerCertificate=yes;"
    )


SRC_CONN = conn_str("SRC")
DST_CONN = conn_str("DST")
BATCH_SIZE = int(os.getenv("RUSGUARD_BATCH_SIZE", "5000"))
POLL_SEC = float(os.getenv("RUSGUARD_POLL_SEC", "15"))
HEARTBEAT_SEC = float(os.getenv("RUSGUARD_HEARTBEAT_SEC", "15"))
BOOTSTRAP_FROM_DEST_MAX = os.getenv("RUSGUARD_BOOTSTRAP_FROM_DEST_MAX", "1").strip().lower() in {"1","true","yes","on"}
GATE_NAME = os.getenv("RUSGUARD_GATE_NAME", "ворота змк юг")
STATE_KEY = os.getenv("RUSGUARD_STATE_KEY", "LAST_RUSGUARD_EXTERNAL_ID_V2")
STATE_TABLE = os.getenv("KPP_STATE_TABLE", "dbo.KPP_RuntimeState")
DEST_TABLE = os.getenv("RUSGUARD_DEST_TABLE", "dbo.RusGuardLogs")
LOCK_PATH = os.getenv("RUSGUARD_LOCK_FILE", str(ROOT / "runtime" / "rusguard_sync_v2.lock"))

SELECT_SQL = f"""
SELECT TOP ({BATCH_SIZE})
  [Log].[_id] AS ExternalId2,
  [Log].[KeyNumber] AS PassExternalId2,
  [Log].[EmployeeID] AS ExternalUserGuid,
  [Log].[DateTime] AS CreatedAt,
  COALESCE(P1.Value,P2.Value) AS PersonControlDeviceName,
  CASE
    WHEN LogMsgSubtypes.Name LIKE N'Вход%' OR LogMsgSubtypes.Name LIKE N'Въезд%' THEN 'IN'
    WHEN LogMsgSubtypes.Name LIKE N'Выход%' OR LogMsgSubtypes.Name LIKE N'Выезд%' THEN 'OUT'
    ELSE 'UNKNOWN'
  END AS Direction,
  LogMsgSubtypes.Name AS ExternalStatus,
  [Log].LogMessageSubType AS ExternalStatusId,
  COALESCE(Employee.LastName,'') + ' ' + COALESCE(Employee.FirstName,'') + ' ' + COALESCE(Employee.SecondName,'') AS FullName,
  EmployeeGroup.Name AS FirmName,
  CardType.Name AS ExternalPassTypeName,
  AcsKeys.Name AS KeyName,
  COALESCE(TRY_PARSE(AcsKeys.Name AS INT),TRY_PARSE(Employee.FirstName AS INT)) AS CardNumParsed,
  [Log].KeyNumber / 65536 AS CardNumReal
FROM [Log]
JOIN LogMsgSubtypes ON [Log].LogMessageSubType=LogMsgSubtypes.Id
LEFT JOIN Employee ON [Log].EmployeeID=Employee._id
LEFT JOIN EmployeeGroup ON Employee.EmployeeGroupID=EmployeeGroup._id
LEFT JOIN AcsKeys ON [Log].KeyNumber=AcsKeys.KeyNumber
LEFT JOIN AcsKey2EmployeeAssignment ON AcsKeys.KeyNumber=AcsKey2EmployeeAssignment.AcsKeyId
LEFT JOIN CardType ON AcsKeys.CardTypeID=CardType._id
LEFT JOIN Property P1 ON [Log].DriverID=P1._idResource AND P1.PropertyName='HardwareName'
LEFT JOIN Property P2 ON [Log].DriverID=P2._idResource AND P2.PropertyName='Name'
WHERE [Log]._id > ?
  AND LogMsgSubtypes.Name IN (
    N'Вход',N'Вход с подтверждением',N'Вход по ключу',N'Въезд',N'Въезд по ключу',N'Въезд с подтверждением',N'Вход по лицу',N'Въезд по лицу',
    N'Выход',N'Выход по считывателю картоприёмника',N'Выход с подтверждением',N'Выход по считывателю картоприёмника с подтверждением',
    N'Выход по ключу',N'Выезд',N'Выезд по считывателю картоприёмника',N'Выезд по ключу',N'Выезд с подтверждением',N'Выезд по считывателю картоприёмника с подтверждением',N'Выход по лицу',N'Выезд по лицу'
  )
  AND LOWER(LTRIM(RTRIM(COALESCE(P1.Value,P2.Value)))) = LOWER(?)
ORDER BY [Log]._id ASC;
"""


def get_state(conn: pyodbc.Connection) -> Optional[int]:
    cur = conn.cursor()
    cur.execute(f"SELECT StateValue FROM {STATE_TABLE} WHERE StateKey=?", STATE_KEY)
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    try:
        return int(str(row[0]).strip())
    except ValueError:
        return None


def destination_max_id(conn: pyodbc.Connection) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT ISNULL(MAX(ExternalId2),0) FROM {DEST_TABLE}")
    row = cur.fetchone()
    return int(row[0] or 0)


def resolve_cursor(conn: pyodbc.Connection) -> Tuple[int, bool]:
    saved = get_state(conn)
    dest_max = destination_max_id(conn)
    if saved is None:
        value = dest_max if BOOTSTRAP_FROM_DEST_MAX else 0
        set_state(conn, value)
        conn.commit()
        log.warning("RusGuard cursor отсутствовал: инициализирован значением %s из приемной БД", value)
        return value, True
    if BOOTSTRAP_FROM_DEST_MAX and dest_max > saved:
        set_state(conn, dest_max)
        conn.commit()
        log.warning(
            "RusGuard cursor %s отставал от уже заполненной приемной БД %s; поднят до MAX(ExternalId2), повторный backfill остановлен",
            saved, dest_max,
        )
        return dest_max, True
    return saved, False


def set_state(conn: pyodbc.Connection, value: int) -> None:
    conn.cursor().execute(
        f"""
MERGE {STATE_TABLE} t USING(SELECT ? StateKey,? StateValue)s ON t.StateKey=s.StateKey
WHEN MATCHED THEN UPDATE SET StateValue=s.StateValue,UpdatedAt=SYSDATETIME()
WHEN NOT MATCHED THEN INSERT(StateKey,StateValue,UpdatedAt) VALUES(s.StateKey,s.StateValue,SYSDATETIME());
""",
        STATE_KEY,
        str(value),
    )


def sync_page() -> Tuple[int, int]:
    with pyodbc.connect(DST_CONN, autocommit=False, timeout=15) as dst:
        cursor_value, _bootstrapped = resolve_cursor(dst)
        with pyodbc.connect(SRC_CONN, autocommit=True, timeout=15) as src:
            cur_src = src.cursor()
            cur_src.execute(SELECT_SQL, cursor_value, GATE_NAME)
            rows = cur_src.fetchall()
        if not rows:
            return 0, cursor_value
        cur_dst = dst.cursor()
        max_id = cursor_value
        for row in rows:
            max_id = max(max_id, int(row.ExternalId2))
            cur_dst.execute(
                f"""
IF NOT EXISTS(SELECT 1 FROM {DEST_TABLE} WHERE ExternalId2=?)
INSERT INTO {DEST_TABLE}(
 ExternalId2,PassExternalId2,ExternalUserGuid,CreatedAt,PersonControlDeviceName,Direction,
 ExternalStatus,ExternalStatusId,FullName,FirmName,ExternalPassTypeName,KeyName,CardNumParsed,CardNumReal,ReceivedAt
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,SYSDATETIME());
""",
                int(row.ExternalId2),
                int(row.ExternalId2), row.PassExternalId2, str(row.ExternalUserGuid) if row.ExternalUserGuid else None,
                row.CreatedAt, row.PersonControlDeviceName, row.Direction, row.ExternalStatus, row.ExternalStatusId,
                row.FullName, row.FirmName, row.ExternalPassTypeName, row.KeyName, row.CardNumParsed, row.CardNumReal,
            )
        set_state(dst, max_id)
        dst.commit()
        log.info("RusGuard COMMIT: rows=%s cursor=%s last_event=%s", len(rows), max_id, rows[-1].CreatedAt)
        return len(rows), max_id


def main() -> int:
    instance_lock = SingleInstanceLock(LOCK_PATH)
    last_heartbeat = 0.0
    total_rows = 0
    last_cursor = 0
    log.info("RusGuard Sync v3.4 запущен; повторный импорт уже имеющихся строк отключён")
    while True:
        try:
            count, last_cursor = sync_page()
            total_rows += count
            now = time.monotonic()
            if count >= BATCH_SIZE:
                continue
            if now - last_heartbeat >= HEARTBEAT_SEC:
                log.info(
                    "STATUS cursor=%s new_rows_last_poll=%s total_this_run=%s next_poll=%.0fs",
                    last_cursor, count, total_rows, POLL_SEC,
                )
                last_heartbeat = now
            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            return 0
        except Exception:
            log.exception("RusGuard sync failed; cursor не изменён")
            time.sleep(max(5.0, POLL_SEC))


if __name__ == "__main__":
    raise SystemExit(main())
