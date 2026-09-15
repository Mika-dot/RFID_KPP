#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Production WEB wrapper v3.4.2.

The operational report is the union of two independent factual sources:
1) confirmed RFID/KPP reel passages;
2) address-warehouse rows from dbo.Warehouse.

A Warehouse row is included even when RFID missed the reel. For report/UI
semantics that row confirms an outbound workshop -> warehouse passage.
"""
from __future__ import annotations

import threading
from datetime import datetime, date
from typing import Any, Dict, List

import kpp_reel_dashboard_v3_ru as base


# WEB is read-only. Avoid SELECT/RECHECK deadlocks while the aggregator repairs a
# large historical queue.
_original_db_connect = base.db_connect


def db_connect_readonly():
    conn = _original_db_connect()
    cur = conn.cursor()
    cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
    cur.execute("SET LOCK_TIMEOUT 5000")
    return conn


base.db_connect = db_connect_readonly


# Make the manager-facing meaning explicit: the address warehouse is an
# independent confirmation that the reel left the workshop even if RFID missed.
base.PAGE = base.PAGE.replace(
    "if (data.SessionCloseReason === 'WAREHOUSE_ONLY') return 'НА СКЛАДЕ БЕЗ КПП';",
    "if (data.SessionCloseReason === 'WAREHOUSE_ONLY') return 'ВЫЕЗД • ПОДТВЕРЖДЕНО АДРЕСНЫМ СКЛАДОМ';",
)


def fetch_report_records(date_from: date, date_to: date) -> List[Dict[str, Any]]:
    """Return KPP RFID UNION address-Warehouse facts without requiring KPP_ReelEvents.

    Warehouse is queried directly, not through KPP_ReelEvents. This is important:
    if RFID never saw the tag, the warehouse fact must still appear in the report.
    """
    start = datetime.combine(date_from, datetime.min.time())
    end = datetime.combine(date_to, datetime.min.time())

    warehouse_query = f"""
    SELECT
        COALESCE(e.EventId, -CONVERT(bigint,w.Id)) AS EventId,
        COALESCE(e.EventKey, CONCAT('WAREHOUSE_DIRECT_',CONVERT(varchar(32),w.Id))) AS EventKey,
        UPPER(LTRIM(RTRIM(ISNULL(w.Tag,'')))) AS SourceTag,
        COALESCE(e.EPC, LEFT(UPPER(LTRIM(RTRIM(ISNULL(w.Tag,'')))),24)) AS EPC,
        COALESCE(e.TID, NULLIF(SUBSTRING(UPPER(LTRIM(RTRIM(ISNULL(w.Tag,'')))),25,256),'')) AS TID,
        COALESCE(e.FirstSeen,w.Dt) AS FirstSeen,
        COALESCE(e.LastSeen,w.Dt) AS LastSeen,
        CASE WHEN e.FinalDirection IS NULL OR e.FinalDirection='UNKNOWN' THEN 'OUT' ELSE e.FinalDirection END AS FinalDirection,
        COALESCE(e.Task1CId,tm.Id) AS Task1CId,
        COALESCE(e.Task1CDt,tm.Dt) AS Task1CDt,
        COALESCE(e.Task1CDocIds,tm.Ids) AS Task1CDocIds,
        COALESCE(et.SeriesNumber,tm.SeriesNumber,'') AS Task1CSeriesNumber,
        COALESCE(e.TaskMatchType,CASE WHEN tm.Id IS NULL THEN 'FULL_TAG_WAREHOUSE' ELSE 'FULL_TAG_BOTH' END) AS TaskMatchType,
        ISNULL(e.RfidReadCount,0) AS RfidReadCount,
        COALESCE(e.SessionCloseReason,'WAREHOUSE_ONLY') AS SessionCloseReason,
        w.Id AS WarehouseId,
        w.Dt AS WarehouseDt,
        w.Ids AS WarehouseDocIds,
        COALESCE(w.SeriesNumber,'') AS WarehouseSeriesNumber,
        COALESCE(e.ReelClassification,CASE WHEN tm.Id IS NULL THEN 'FULL_TAG_WAREHOUSE' ELSE 'FULL_TAG_BOTH' END) AS ReelClassification,
        e.PassageGroupKey,
        e.GroupReelCount
    FROM dbo.Warehouse w
    OUTER APPLY (
        SELECT TOP(1)
            e0.EventId,e0.EventKey,e0.EPC,e0.TID,e0.FirstSeen,e0.LastSeen,e0.FinalDirection,
            e0.Task1CId,e0.Task1CDt,e0.Task1CDocIds,e0.TaskMatchType,e0.RfidReadCount,
            e0.SessionCloseReason,e0.ReelClassification,e0.PassageGroupKey,e0.GroupReelCount
        FROM dbo.KPP_ReelEvents e0
        WHERE UPPER(LTRIM(RTRIM(ISNULL(e0.SourceTag,''))))=UPPER(LTRIM(RTRIM(ISNULL(w.Tag,''))))
          AND ISNULL(e0.RfidReadCount,0)>0
          AND e0.FirstSeen BETWEEN DATEADD(hour,-24,w.Dt) AND DATEADD(hour,24,w.Dt)
        ORDER BY ABS(DATEDIFF(second,e0.FirstSeen,w.Dt)),e0.EventId DESC
    ) e
    LEFT JOIN {base.Config.TASK_TABLE} et ON et.Id=e.Task1CId
    OUTER APPLY (
        SELECT TOP(1) t0.Id,t0.Dt,t0.Ids,t0.SeriesNumber
        FROM {base.Config.TASK_TABLE} t0
        WHERE UPPER(LTRIM(RTRIM(ISNULL(t0.Tag,''))))=UPPER(LTRIM(RTRIM(ISNULL(w.Tag,''))))
          AND t0.Dt BETWEEN DATEADD(hour,-24,w.Dt) AND DATEADD(hour,24,w.Dt)
        ORDER BY ABS(DATEDIFF(second,t0.Dt,w.Dt)),t0.Id DESC
    ) tm
    WHERE w.Dt>=? AND w.Dt<DATEADD(day,1,?)
      AND NULLIF(LTRIM(RTRIM(ISNULL(w.SeriesNumber,''))),'') IS NOT NULL
    ORDER BY w.Dt,w.SeriesNumber,w.Id;
    """

    kpp_query = f"""
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
    WHERE e.FirstSeen>=? AND e.FirstSeen<DATEADD(day,1,?)
      AND e.IsReel=1 AND ISNULL(e.RfidReadCount,0)>0
    ORDER BY e.FirstSeen,e.EventId;
    """

    with base.db_connect() as conn:
        cur = conn.cursor()
        cur.execute(warehouse_query, start, end)
        warehouse_rows = [base.row_to_dict(cur, row) for row in cur.fetchall()]

        matched_event_ids = {
            int(r["EventId"])
            for r in warehouse_rows
            if r.get("EventId") is not None and int(r["EventId"]) > 0
        }

        cur.execute(kpp_query, start, end)
        kpp_rows = [base.row_to_dict(cur, row) for row in cur.fetchall()]

    # Warehouse rows are authoritative for warehouse arrival and are emitted
    # first. Add only RFID passages that were not already paired with a
    # warehouse fact, preventing double counting.
    records = list(warehouse_rows)
    records.extend(r for r in kpp_rows if int(r.get("EventId") or 0) not in matched_event_ids)
    records.sort(
        key=lambda r: (
            r.get("WarehouseDt") or r.get("FirstSeen") or datetime.min,
            str(r.get("WarehouseSeriesNumber") or r.get("Task1CSeriesNumber") or ""),
            int(r.get("EventId") or 0),
        )
    )
    return records


base.fetch_report_records = fetch_report_records


if __name__ == "__main__":
    instance_lock = base.SingleInstanceLock(base.Config.LOCK_PATH)
    if not base.Config.DB_CONN_STR:
        raise RuntimeError("Не задан KPP_WEB_DB_CONNECTION / RFID_DB_CONNECTION")
    if base.Config.AUTH_REQUIRED and (not base.Config.AUTH_USER or not base.Config.AUTH_PASSWORD):
        raise RuntimeError("При KPP_WEB_AUTH_REQUIRED=1 задайте KPP_WEB_AUTH_USER и KPP_WEB_AUTH_PASSWORD")
    print("\n" + "█" * 100)
    print("🌐 КПП • WEB v3.4.2: отчёт = RFID КПП ∪ адресный склад (прямой источник)")
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
