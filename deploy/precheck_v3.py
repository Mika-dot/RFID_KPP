#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only preflight for RFID KPP v3.2."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path


REQUIRED_ENV = [
    "KPP_CONN_STR",
    "KPP_TASK_CONN_STR",
    "KPP_WEB_DB_CONNECTION",
    "RFID_DB_CONNECTION",
    "RFID_DLL_PATH",
    "RFID_READER_IP",
    "RFID_MODEL_PATH",
    "RFID_RTSP_0",
    "RFID_RTSP_1",
    "SRC_SERVER",
    "SRC_DATABASE",
    "SRC_USERNAME",
    "SRC_PASSWORD",
    "DST_SERVER",
    "DST_DATABASE",
    "DST_USERNAME",
    "DST_PASSWORD",
]

REQUIRED_COLUMNS = {
    "dbo.RFID_Tags": ["ClientReadUuid", "SourceReaderTime", "ReceivedAt", "IngestBatchId", "TimeQuality"],
    "dbo.ReelTransitions": ["ClientEventUuid", "CapturedAt", "ProcessedAt", "ImageData", "ReelCount"],
    "dbo.KPP_ReelEvents": ["IsReel", "ObjectType", "WarehouseId", "PassageGroupKey", "RfidMinRawId", "ProcessingVersion"],
}
REQUIRED_TABLES = [
    "dbo.KPP_RuntimeState",
    "dbo.KPP_ActiveRfidSessions",
    "dbo.KPP_EventVideoLinks",
    "dbo.KPP_EventSkudLinks",
    "dbo.KPP_ProcessingErrors",
]


def fail(message: str, errors: list[str]) -> None:
    errors.append(message)
    print(f"[ERROR] {message}")


def main() -> int:
    errors: list[str] = []
    for name in REQUIRED_ENV:
        value = os.getenv(name, "").strip()
        if not value or "<" in value or ">" in value:
            fail(f"Не заполнена переменная {name}", errors)

    if os.getenv("KPP_WEB_AUTH_REQUIRED", "1") == "1":
        for name in ("KPP_WEB_AUTH_USER", "KPP_WEB_AUTH_PASSWORD"):
            value = os.getenv(name, "").strip()
            if not value or "<" in value or ">" in value:
                fail(f"Не заполнена переменная {name}", errors)

    for name in ("RFID_DLL_PATH", "RFID_MODEL_PATH", "RFID_MASK_0", "RFID_MASK_1"):
        value = os.getenv(name, "").strip()
        if value and not Path(value).exists():
            fail(f"Файл {name} не найден: {value}", errors)

    for module in ("pyodbc", "flask", "waitress", "numpy", "cv2", "ultralytics"):
        try:
            importlib.import_module(module)
            print(f"[OK] Python module: {module}")
        except Exception as exc:
            fail(f"Не импортируется {module}: {exc}", errors)

    if not errors:
        try:
            import pyodbc
            with pyodbc.connect(os.environ["KPP_CONN_STR"], timeout=10) as conn:
                cur = conn.cursor()
                for table in REQUIRED_TABLES:
                    cur.execute("SELECT OBJECT_ID(?, 'U')", table)
                    if cur.fetchone()[0] is None:
                        fail(f"Не найдена таблица {table}; примените миграцию", errors)
                for table, columns in REQUIRED_COLUMNS.items():
                    for column in columns:
                        cur.execute("SELECT COL_LENGTH(?, ?)", table, column)
                        if cur.fetchone()[0] is None:
                            fail(f"Не найден столбец {table}.{column}; примените миграцию", errors)
                cur.execute("SELECT StateValue FROM dbo.KPP_RuntimeState WHERE StateKey='LAST_RFID_ID_V3'")
                row = cur.fetchone()
                if not row:
                    fail("Не создан cutover cursor LAST_RFID_ID_V3", errors)
                else:
                    print(f"[OK] LAST_RFID_ID_V3={row[0]}")
                cur.execute("SELECT StateValue FROM dbo.KPP_RuntimeState WHERE StateKey='KPP_SCHEMA_VERSION'")
                schema_row = cur.fetchone()
                if not schema_row or str(schema_row[0]).strip() != "3.2.0":
                    fail("Не установлен KPP_SCHEMA_VERSION=3.2.0", errors)
                else:
                    print("[OK] KPP_SCHEMA_VERSION=3.2.0")
                cur.execute("SELECT COUNT(*) FROM dbo.KPP_ActiveRfidSessions")
                print(f"[OK] Durable active sessions: {cur.fetchone()[0]}")
        except Exception as exc:
            fail(f"Проверка SQL Server не выполнена: {exc}", errors)

    if errors:
        print(f"\nPRECHECK FAILED: {len(errors)} ошибок")
        return 1
    print("\nPRECHECK OK: конфигурация и схема v3.2 готовы к запуску")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
