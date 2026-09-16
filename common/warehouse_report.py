#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure report union for KPP RFID events and address Warehouse rows."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from common.warehouse_identity import (
    IdentityCandidate,
    IdentityRecord,
    MATCH_AMBIGUOUS_SERIES,
    MATCH_IDS,
    MATCH_NONE,
    MATCH_SERIES,
    MATCH_TAG,
    normalize_tag,
    normalize_value,
    resolve_warehouse_identity,
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


def _linked_method(
    warehouse: Mapping[str, Any],
    event: Mapping[str, Any],
    candidates: Sequence[IdentityCandidate],
) -> str:
    """Recover the original TAG/IDS/SERIES method for a persisted DB link."""
    source_tag = normalize_tag(event.get("SourceTag"))
    for candidate in candidates:
        if candidate.tag == source_tag:
            return candidate.method
    if source_tag and source_tag == normalize_tag(warehouse.get("WarehouseTag")):
        return MATCH_TAG
    if (
        normalize_value(event.get("Task1CDocIds"))
        and normalize_value(event.get("Task1CDocIds"))
        == normalize_value(warehouse.get("WarehouseDocIds"))
    ):
        return MATCH_IDS
    if (
        normalize_value(event.get("Task1CSeriesNumber"))
        and normalize_value(event.get("Task1CSeriesNumber"))
        == normalize_value(warehouse.get("WarehouseSeriesNumber"))
    ):
        return MATCH_SERIES
    return MATCH_NONE


def build_report_records(
    warehouse_rows: Iterable[Mapping[str, Any]],
    task_rows: Iterable[Mapping[str, Any]],
    kpp_rows: Iterable[Mapping[str, Any]],
    start: datetime,
    end_exclusive: datetime,
) -> List[Dict[str, Any]]:
    """Build ``RFID KPP union Warehouse`` using TAG -> IDS -> SERIES.

    A persisted ``KPP_ReelEvents.WarehouseId`` is authoritative and therefore
    wins over a closer, still-unlinked event. This prevents report duplicates
    during cursor replay and after a service restart.
    """
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
    events_by_warehouse: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for event in events:
        tag = normalize_tag(event.get("SourceTag"))
        if tag:
            events_by_tag[tag].append(event)
        if event.get("WarehouseId") is not None:
            events_by_warehouse[int(event["WarehouseId"])].append(event)

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

        exact = [
            event
            for event in events_by_warehouse.get(warehouse_id, ())
            if int(event.get("EventId") or 0) not in used_event_ids
            and event.get("FirstSeen") is not None
        ]
        matched = min(
            exact,
            key=lambda event: (
                abs((event["FirstSeen"] - warehouse_dt).total_seconds()),
                -int(event.get("EventId") or 0),
            ),
        ) if exact else None
        matched_candidate = None
        if matched is not None:
            matched_method = _linked_method(warehouse, matched, resolution.candidates)
        else:
            matched_method = MATCH_NONE
            for candidate in resolution.candidates:
                possible = [
                    event
                    for event in events_by_tag.get(candidate.tag, ())
                    if int(event.get("EventId") or 0) not in used_event_ids
                    and event.get("FirstSeen") is not None
                    and abs(event["FirstSeen"] - warehouse_dt) <= max_delta
                    and event.get("WarehouseId") is None
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
                    matched_method = candidate.method
                    break

        if matched is not None:
            record = dict(matched)
            used_event_ids.add(int(record["EventId"]))
            task = (matched_candidate.task if matched_candidate else None) or resolution.primary_task
            record.update(
                {
                    "Task1CId": record.get("Task1CId") or (task.row_id if task else None),
                    "Task1CDt": record.get("Task1CDt") or (task.dt if task else None),
                    "Task1CDocIds": record.get("Task1CDocIds") or (task.ids if task else ""),
                    "Task1CSeriesNumber": record.get("Task1CSeriesNumber") or (task.series_number if task else ""),
                    "WarehouseMatchMethod": matched_method,
                    "WarehouseLinkStatus": f"MATCH_{matched_method}",
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
