#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Production WEB wrapper v3.4.4.

The operational report is the union of two independent factual sources:
1) confirmed RFID/KPP reel passages;
2) address-warehouse rows from dbo.Warehouse.

A Warehouse row is included even when RFID missed the reel. For report/UI
semantics that row confirms an outbound workshop -> warehouse passage. Tag is
optional; Ids and SeriesNumber are the mandatory 1C identity.
"""
from __future__ import annotations

import os
import threading
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import kpp_reel_dashboard_v3_ru as base
from common.warehouse_identity import (
    IdentityRecord,
    MATCH_AMBIGUOUS_SERIES,
    MATCH_NONE,
    normalize_tag,
    normalize_value,
    resolve_warehouse_identity,
)


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


def _task_record(row: Mapping[str, Any]) -> IdentityRecord:
    return IdentityRecord.from_values(
        row["Id"], row["Dt"], row.get("Tag"), row.get("Ids"), row.get("SeriesNumber")
    )


def _task_pool(
    warehouse: Mapping[str, Any],
    by_tag: Mapping[str, Sequence[IdentityRecord]],
    by_ids: Mapping[str, Sequence[IdentityRecord]],
    by_series: Mapping[str, Sequence[IdentityRecord]],
) -> List[IdentityRecord]:
    values: Dict[int, IdentityRecord] = {}
    for row in by_tag.get(normalize_tag(warehouse.get("WarehouseTag")), ()):
        values[row.row_id] = row
    for row in by_ids.get(normalize_value(warehouse.get("WarehouseDocIds")), ()):
        values[row.row_id] = row
    for row in by_series.get(normalize_value(warehouse.get("WarehouseSeriesNumber")), ()):
        values[row.row_id] = row
    return list(values.values())


def build_report_records(
    warehouse_rows: Iterable[Mapping[str, Any]],
    task_rows: Iterable[Mapping[str, Any]],
    kpp_rows: Iterable[Mapping[str, Any]],
    start: datetime,
    end_exclusive: datetime,
) -> List[Dict[str, Any]]:
    """Build ``RFID KPP union Warehouse`` using TAG -> IDS -> SERIES."""
    tasks = [_task_record(row) for row in task_rows if row.get("Dt") is not None]
    by_tag: Dict[str, List[IdentityRecord]] = defaultdict(list)
    by_ids: Dict[str, List[IdentityRecord]] = defaultdict(list)
    by_series: Dict[str, List[IdentityRecord]] = defaultdict(list)
    for row in tasks:
        if row.tag:
            by_tag[row.tag].append(row)
        if row.ids:
            by_ids[row.ids].append(row)
        if row.series_number:
            by_series[row.series_number].append(row)

    events = [dict(row) for row in kpp_rows]
    events_by_tag: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for event in events:
        tag = normalize_tag(event.get("SourceTag"))
        if tag:
            events_by_tag[tag].append(event)

    used_event_ids = set()
    records: List[Dict[str, Any]] = []
    max_delta = timedelta(hours=24)
    ordered_warehouse = sorted(
        (dict(row) for row in warehouse_rows),
        key=lambda row: (row.get("WarehouseDt") or datetime.min, int(row.get("WarehouseId") or 0)),
    )
    for warehouse in ordered_warehouse:
        warehouse_id = int(warehouse["WarehouseId"])
        warehouse_dt = warehouse["WarehouseDt"]
        resolution = resolve_warehouse_identity(
            warehouse.get("WarehouseTag"),
            warehouse.get("WarehouseDocIds"),
            warehouse.get("WarehouseSeriesNumber"),
            warehouse_dt,
            _task_pool(warehouse, by_tag, by_ids, by_series),
            24,
        )

        matched = None
        matched_candidate = None
        for candidate in resolution.candidates:
            possible = [
                event
                for event in events_by_tag.get(candidate.tag, ())
                if int(event.get("EventId") or 0) not in used_event_ids
                and event.get("FirstSeen") is not None
                and abs(event["FirstSeen"] - warehouse_dt) <= max_delta
                and (
                    event.get("WarehouseId") is None
                    or int(event.get("WarehouseId") or 0) == warehouse_id
                )
            ]
            if possible:
                matched = min(
                    possible,
                    key=lambda event: (
                        abs((event["FirstSeen"] - warehouse_dt).total_seconds()),
                        -int(event.get("EventId") or 0),
                    ),
                )
                matched_candidate = candidate
                break

        if matched is not None:
            record = dict(matched)
            used_event_ids.add(int(record["EventId"]))
            task = matched_candidate.task or resolution.primary_task
            record.update(
                {
                    "Task1CId": record.get("Task1CId") or (task.row_id if task else None),
                    "Task1CDt": record.get("Task1CDt") or (task.dt if task else None),
                    "Task1CDocIds": record.get("Task1CDocIds") or (task.ids if task else ""),
                    "Task1CSeriesNumber": record.get("Task1CSeriesNumber") or (task.series_number if task else ""),
                    "WarehouseMatchMethod": matched_candidate.method,
                    "WarehouseLinkStatus": f"MATCH_{matched_candidate.method}",
                    "FinalDirection": (
                        "OUT"
                        if record.get("FinalDirection") in (None, "", "UNKNOWN")
                        else record["FinalDirection"]
                    ),
                }
            )
        else:
            task = resolution.primary_task
            method = resolution.candidates[0].method if resolution.candidates else resolution.primary_method
            link_status = (
                MATCH_AMBIGUOUS_SERIES
                if resolution.series_ambiguous and not resolution.candidates
                else "WAREHOUSE_ONLY"
            )
            resolved_tag = resolution.preferred_tag
            record = {
                "EventId": -warehouse_id,
                "EventKey": f"WAREHOUSE_DIRECT_{warehouse_id}",
                "SourceTag": resolved_tag,
                "EPC": resolved_tag[:24],
                "TID": resolved_tag[24:] or None,
                "FirstSeen": warehouse_dt,
                "LastSeen": warehouse_dt,
                "FinalDirection": "OUT",
                "Task1CId": task.row_id if task else None,
                "Task1CDt": task.dt if task else None,
                "Task1CDocIds": task.ids if task else "",
                "Task1CSeriesNumber": task.series_number if task else "",
                "TaskMatchType": "WAREHOUSE_ONLY",
                "RfidReadCount": 0,
                "SessionCloseReason": "WAREHOUSE_ONLY",
                "ReelClassification": "WAREHOUSE_ONLY",
                "PassageGroupKey": None,
                "GroupReelCount": None,
                "WarehouseMatchMethod": method,
                "WarehouseLinkStatus": link_status,
            }
        record.update(
            {
                "WarehouseId": warehouse_id,
                "WarehouseDt": warehouse_dt,
                "WarehouseDocIds": warehouse.get("WarehouseDocIds") or "",
                "WarehouseSeriesNumber": warehouse.get("WarehouseSeriesNumber") or "",
                "WarehouseTag": warehouse.get("WarehouseTag") or "",
                "WarehouseSeriesAmbiguous": resolution.series_ambiguous,
            }
        )
        records.append(record)

    for event in events:
        event_id = int(event.get("EventId") or 0)
        first_seen = event.get("FirstSeen")
        if event_id in used_event_ids or first_seen is None or not (start <= first_seen < end_exclusive):
            continue
        record = dict(event)
        record.setdefault("WarehouseSeriesNumber", "")
        record["WarehouseMatchMethod"] = MATCH_NONE
        record["WarehouseLinkStatus"] = "KPP_ONLY"
        record["WarehouseSeriesAmbiguous"] = False
        records.append(record)

    records.sort(
        key=lambda row: (
            row.get("WarehouseDt") or row.get("FirstSeen") or datetime.min,
            str(row.get("WarehouseSeriesNumber") or row.get("Task1CSeriesNumber") or ""),
            int(row.get("EventId") or 0),
        )
    )
    return records


def fetch_report_records(date_from: date, date_to: date) -> List[Dict[str, Any]]:
    """Return KPP RFID UNION Warehouse; Warehouse.Tag may be NULL."""
    start = datetime.combine(date_from, datetime.min.time())
    end_exclusive = datetime.combine(date_to + timedelta(days=1), datetime.min.time())
    expanded_start = start - timedelta(hours=24)
    expanded_end = end_exclusive + timedelta(hours=24)
    warehouse_table = os.getenv("KPP_WAREHOUSE_TABLE", "dbo.Warehouse")

    warehouse_query = f"""
    SELECT Id AS WarehouseId,Dt AS WarehouseDt,Tag AS WarehouseTag,
           Ids AS WarehouseDocIds,SeriesNumber AS WarehouseSeriesNumber
    FROM {warehouse_table}
    WHERE Dt>=? AND Dt<?
      AND NULLIF(LTRIM(RTRIM(ISNULL(Ids,''))),'') IS NOT NULL
      AND NULLIF(LTRIM(RTRIM(ISNULL(SeriesNumber,''))),'') IS NOT NULL
    ORDER BY Dt,Id;
    """
    task_query = f"""
    SELECT Id,Dt,Tag,Ids,SeriesNumber
    FROM {base.Config.TASK_TABLE}
    WHERE Dt>=? AND Dt<?
      AND NULLIF(LTRIM(RTRIM(ISNULL(Tag,''))),'') IS NOT NULL;
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
    LEFT JOIN {warehouse_table} w ON w.Id=e.WarehouseId
    WHERE e.FirstSeen>=? AND e.FirstSeen<?
      AND e.IsReel=1 AND ISNULL(e.RfidReadCount,0)>0
      AND ISNULL(e.SessionCloseReason,'')<>'WAREHOUSE_ONLY'
    ORDER BY e.FirstSeen,e.EventId;
    """

    with base.db_connect() as conn:
        cur = conn.cursor()
        cur.execute(warehouse_query, start, end_exclusive)
        warehouse_rows = [base.row_to_dict(cur, row) for row in cur.fetchall()]
        cur.execute(task_query, expanded_start, expanded_end)
        task_rows = [base.row_to_dict(cur, row) for row in cur.fetchall()]
        cur.execute(kpp_query, expanded_start, expanded_end)
        kpp_rows = [base.row_to_dict(cur, row) for row in cur.fetchall()]

    return build_report_records(warehouse_rows, task_rows, kpp_rows, start, end_exclusive)


base.fetch_report_records = fetch_report_records


if __name__ == "__main__":
    instance_lock = base.SingleInstanceLock(base.Config.LOCK_PATH)
    if not base.Config.DB_CONN_STR:
        raise RuntimeError("Не задан KPP_WEB_DB_CONNECTION / RFID_DB_CONNECTION")
    if base.Config.AUTH_REQUIRED and (not base.Config.AUTH_USER or not base.Config.AUTH_PASSWORD):
        raise RuntimeError("При KPP_WEB_AUTH_REQUIRED=1 задайте KPP_WEB_AUTH_USER и KPP_WEB_AUTH_PASSWORD")
    print("\n" + "█" * 100)
    print("🌐 КПП • WEB v3.4.4: Warehouse Tag/Ids/SeriesNumber identity")
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
