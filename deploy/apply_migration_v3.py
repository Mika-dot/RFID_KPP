#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Idempotent one-click SQL migration for RFID KPP v3.4.5."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "001_kpp_v3_reliability.sql"
EXPECTED_VERSION = "3.4.5"


def complete(cur) -> bool:
    checks = [
        ("dbo.RFID_Tags", "ClientReadUuid"),
        ("dbo.RFID_Tags", "SourceReaderTime"),
        ("dbo.ReelTransitions", "CapturedAt"),
        ("dbo.ReelTransitions", "ImageData"),
        ("dbo.KPP_ReelEvents", "IsReel"),
        ("dbo.KPP_ReelEvents", "PassageGroupKey"),
    ]
    for table, column in checks:
        cur.execute("SELECT COL_LENGTH(?, ?)", table, column)
        if cur.fetchone()[0] is None:
            return False
    for table in (
        "dbo.KPP_ActiveRfidSessions",
        "dbo.KPP_EventVideoLinks",
        "dbo.KPP_EventSkudLinks",
        "dbo.KPP_ProcessingErrors",
    ):
        cur.execute("SELECT OBJECT_ID(?, 'U')", table)
        if cur.fetchone()[0] is None:
            return False
    cur.execute("SELECT OBJECT_ID('dbo.KPP_RuntimeState', 'U')")
    if cur.fetchone()[0] is None:
        return False
    cur.execute(
        """
SELECT t.name
FROM sys.columns c
JOIN sys.types t ON t.user_type_id=c.user_type_id
WHERE c.object_id=OBJECT_ID('dbo.KPP_ReelEvents') AND c.name='WarehouseId'
"""
    )
    warehouse_id_type = cur.fetchone()
    if not warehouse_id_type or str(warehouse_id_type[0]).lower() != "bigint":
        return False
    cur.execute("SELECT StateValue FROM dbo.KPP_RuntimeState WHERE StateKey='KPP_SCHEMA_VERSION'")
    row = cur.fetchone()
    return bool(row and str(row[0]).strip() == EXPECTED_VERSION)


def split_batches(sql: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?im)^\s*GO\s*(?:--.*)?$", sql) if part.strip()]


def apply_with_connection(conn_str: str, label: str) -> tuple[bool, str]:
    import pyodbc
    try:
        with pyodbc.connect(conn_str, autocommit=True, timeout=15) as conn:
            cur = conn.cursor()
            try:
                cur.timeout = 0
            except Exception:
                pass
            if complete(cur):
                print(f"[OK] SQL schema {EXPECTED_VERSION} is already installed ({label}).")
                return True, ""
            batches = split_batches(MIGRATION.read_text(encoding="utf-8-sig"))
            print(f"[INFO] Applying SQL migration using {label}: {len(batches)} batches...")
            for index, batch in enumerate(batches, 1):
                try:
                    cur.execute(batch)
                    while cur.nextset():
                        pass
                except Exception as exc:
                    return False, f"batch {index}/{len(batches)}: {exc}"
            if not complete(cur):
                return False, "migration finished but required schema objects are missing"
            print(f"[OK] SQL migration {EXPECTED_VERSION} applied ({label}).")
            return True, ""
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    primary = os.getenv("KPP_CONN_STR", "").strip()
    trusted = os.getenv("KPP_MIGRATION_TRUSTED_CONN", "").strip()
    if not primary:
        print("[FATAL] KPP_CONN_STR is empty.")
        return 2
    if not MIGRATION.exists():
        print(f"[FATAL] Migration file not found: {MIGRATION}")
        return 3

    candidates = [("configured SQL login", primary)]
    if trusted and trusted != primary:
        candidates.append(("Windows trusted connection", trusted))

    errors: list[str] = []
    for label, conn_str in candidates:
        ok, error = apply_with_connection(conn_str, label)
        if ok:
            return 0
        errors.append(f"{label}: {error}")
        print(f"[WARN] Migration attempt failed ({label}): {error}")

    print("[FATAL] SQL migration was not applied.")
    for error in errors:
        print(f"  - {error}")
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
