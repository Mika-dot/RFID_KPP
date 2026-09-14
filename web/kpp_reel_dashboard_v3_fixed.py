#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Production wrapper for WEB v3.4.

Fixes the RFID/Warehouse report so the report is a union of confirmed KPP reels
and Warehouse-only reels instead of requiring RfidReadCount > 0.
"""
from __future__ import annotations

import threading
from datetime import datetime, date
from typing import Any, Dict, List

import kpp_reel_dashboard_v3_ru as base


def fetch_report_records(date_from: date, date_to: date) -> List[Dict[str, Any]]:
    query = f"""
    SELECT
        e.EventId,e.EventKey,e.SourceTag,e.EPC,e.TID,e.FirstSeen,e.LastSeen,e.FinalDirection,
        e.Task1CId,e.Task1CDt,e.Task1CDocIds,t.SeriesNumber AS Task1CSeriesNumber,
        e.TaskMatchType,e.RfidReadCount,e.SessionCloseReason,
        e.WarehouseId,e.WarehouseDt,e.WarehouseDocIds,
        COALESCE(w.SeriesNumber,'') AS WarehouseSeriesNumber,
        e.ReelClassification,e.PassageGroupKey,e.GroupReelCount
    FROM dbo.KPP_ReelEvents e
    LEFT JOIN {base.Config.TASK_TABLE} t ON t.Id=e.Task1CId
    LEFT JOIN dbo.Warehouse w ON w.Id=e.WarehouseId
    WHERE COALESCE(e.WarehouseDt,e.FirstSeen)>=?
      AND COALESCE(e.WarehouseDt,e.FirstSeen)<DATEADD(day,1,?)
      AND e.IsReel=1
      AND (
          ISNULL(e.RfidReadCount,0)>0
          OR e.WarehouseId IS NOT NULL
          OR e.SessionCloseReason='WAREHOUSE_ONLY'
      )
    ORDER BY COALESCE(e.WarehouseDt,e.FirstSeen),COALESCE(w.SeriesNumber,t.SeriesNumber,''),e.EventId
    """
    with base.db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            query,
            datetime.combine(date_from, datetime.min.time()),
            datetime.combine(date_to, datetime.min.time()),
        )
        return [base.row_to_dict(cur, row) for row in cur.fetchall()]


base.fetch_report_records = fetch_report_records


if __name__ == "__main__":
    instance_lock = base.SingleInstanceLock(base.Config.LOCK_PATH)
    if not base.Config.DB_CONN_STR:
        raise RuntimeError("Не задан KPP_WEB_DB_CONNECTION / RFID_DB_CONNECTION")
    if base.Config.AUTH_REQUIRED and (not base.Config.AUTH_USER or not base.Config.AUTH_PASSWORD):
        raise RuntimeError("При KPP_WEB_AUTH_REQUIRED=1 задайте KPP_WEB_AUTH_USER и KPP_WEB_AUTH_PASSWORD")
    print("\n" + "█" * 100)
    print("🌐 КПП • WEB v3.4.1: отчёт = КПП ∪ адресный склад")
    print("█" * 100)
    print(f"Адрес: {base.Config.HOST}")
    print(f"Порт: {base.Config.PORT}")
    threading.Thread(target=base.web_heartbeat, name="web-heartbeat", daemon=True).start()
    if base.Config.DEBUG:
        base.app.run(host=base.Config.HOST, port=base.Config.PORT, debug=True, use_reloader=False)
    else:
        from waitress import serve
        serve(
            base.app,
            host=base.Config.HOST,
            port=base.Config.PORT,
            threads=base.Config.THREADS,
            channel_timeout=30,
            cleanup_interval=10,
        )
