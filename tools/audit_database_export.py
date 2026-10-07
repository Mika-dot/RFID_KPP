#!/usr/bin/env python3
"""Offline checks of complete Perimeter CSV exports; no database connection.

Raw records and personal RusGuard fields never appear in the output. Identity
and temporal matches are candidates, not accepted passages or WEB proof.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.warehouse_identity import IdentityRecord, normalize_value, resolve_warehouse_identity


FIELDS = {
    "RFID_Tags": ["Id", "RecordTime", "EPC", "TID", "ClientReadUuid", "ReceivedAt",
                  "SourceReaderTime", "SourceSequence", "IngestBatchId", "TimeQuality", "IsProcessed"],
    "RfidTags": ["Id", "Dt", "Tag", "Ids", "SeriesNumber"],
    "Warehouse": ["Id", "Dt", "Tag", "Ids", "SeriesNumber"],
    "RusGuardLogs": ["ExternalId2", "CreatedAt", "Direction"],
    "KPP_EventVideoLinks": ["PassageGroupKey", "VideoEventId", "ReelCount", "LinkedAt"],
    "KPP_EventSkudLinks": ["PassageGroupKey", "SkudExternalId", "LinkedAt"],
    "KPP_ActiveRfidSessions": ["SourceTag", "MinRawId", "MaxRawId", "UpdatedAt"],
}


def dt(value):
    return datetime.fromisoformat(value) if value and value.strip() else None


def excess(values):
    return sum(n - 1 for n in values.values() if n > 1)


def bounds(values):
    values = [x for x in values if x is not None]
    return {"min": min(values).isoformat(), "max": max(values).isoformat()} if values else {"min": None, "max": None}


class Export:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.inventory = {}

    def rows(self, table):
        paths = list(self.folder.glob(table + "_*.csv"))
        if len(paths) != 1:
            raise ValueError("Exactly one complete CSV required for " + table)
        path = paths[0]
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        item = {"filename": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest(), "rows": 0}
        self.inventory[table] = item
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not set(FIELDS[table]).issubset(reader.fieldnames or []):
                raise ValueError("Required columns missing in " + table)
            item["columns"] = reader.fieldnames
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Incomplete CSV record in " + table)
                item["rows"] += 1
                yield row


def audit(folder, cutoff):
    source = Export(folder)
    raw_ids, raw_uuids, raw_sequences = Counter(), Counter(), Counter()
    raw_times, received_times, source_times = [], [], []
    raw_by_tag = defaultdict(list)
    quality, processed, fresh_quality = Counter(), Counter(), Counter()
    missing = Counter()
    fresh_rows = modern_rows = source_after_received = 0
    received_since_hotfix = old_source_received_since_hotfix = incomplete_uuid_metadata = 0
    max_delivery_lag = max_fresh_delivery_lag = None
    delivery_thresholds = Counter()
    for row in source.rows("RFID_Tags"):
        raw_id = int(row["Id"])
        raw_ids[raw_id] += 1
        record = dt(row["RecordTime"])
        received = dt(row["ReceivedAt"])
        source_at = dt(row["SourceReaderTime"])
        raw_times.append(record)
        received_times.append(received)
        source_times.append(source_at)
        quality[row["TimeQuality"] or "<empty>"] += 1
        processed[row["IsProcessed"] or "<empty>"] += 1
        for key in ("ClientReadUuid", "SourceReaderTime", "SourceSequence", "IngestBatchId"):
            missing[key] += not bool(row[key])
        if row["ClientReadUuid"]:
            raw_uuids[normalize_value(row["ClientReadUuid"])] += 1
            modern_rows += 1
            incomplete_uuid_metadata += any(not row[key] for key in ("SourceReaderTime", "SourceSequence", "IngestBatchId"))
        if row["IngestBatchId"] and row["SourceSequence"]:
            raw_sequences[(normalize_value(row["IngestBatchId"]), row["SourceSequence"])] += 1
        if source_at is not None and source_at >= cutoff:
            fresh_rows += 1
            fresh_quality[row["TimeQuality"] or "<empty>"] += 1
        if received is not None and received >= cutoff:
            received_since_hotfix += 1
            old_source_received_since_hotfix += source_at is not None and source_at < cutoff
        if received is not None and source_at is not None:
            lag = (received - source_at).total_seconds()
            source_after_received += lag < 0
            max_delivery_lag = max(max_delivery_lag if max_delivery_lag is not None else lag, lag)
            if source_at >= cutoff:
                max_fresh_delivery_lag = max(max_fresh_delivery_lag if max_fresh_delivery_lag is not None else lag, lag)
            if row["ClientReadUuid"]:
                for seconds in (5, 60, 900):
                    delivery_thresholds["over_" + str(seconds) + "s"] += lag > seconds
        full_tag = normalize_value(row["EPC"]) + normalize_value(row["TID"])
        match_at = source_at or record
        if full_tag and match_at is not None:
            raw_by_tag[full_tag].append(match_at)
    for times in raw_by_tag.values():
        times.sort()
    raw = {"id_duplicates": excess(raw_ids), "id_range": [min(raw_ids), max(raw_ids)] if raw_ids else [],
           "record_time": bounds(raw_times), "received_at": bounds(received_times),
           "source_reader_time": bounds(source_times), "unique_physical_tags": len(raw_by_tag),
           "client_uuid_rows": modern_rows, "client_uuid_duplicates": excess(raw_uuids),
           "client_uuid_rows_with_incomplete_metadata": incomplete_uuid_metadata,
           "batch_sequence_duplicates": excess(raw_sequences), "missing": dict(missing),
           "time_quality": dict(quality), "is_processed": dict(processed),
           "source_rows_since_hotfix": fresh_rows, "source_quality_since_hotfix": dict(fresh_quality),
           "received_rows_since_hotfix": received_since_hotfix,
           "old_source_rows_received_since_hotfix": old_source_received_since_hotfix,
           "max_delivery_lag_for_source_since_hotfix_seconds": max_fresh_delivery_lag,
           "delivery_lag_thresholds_uuid_rows": dict(delivery_thresholds),
           "source_after_received_rows": source_after_received, "max_delivery_lag_seconds": max_delivery_lag}

    task_ids, task_keys = Counter(), Counter()
    task_times = []
    by_tag, by_ids, by_series = defaultdict(list), defaultdict(list), defaultdict(list)
    empty_task_tags = 0
    for row in source.rows("RfidTags"):
        record = IdentityRecord.from_values(row["Id"], dt(row["Dt"]), row["Tag"], row["Ids"], row["SeriesNumber"])
        task_ids[record.row_id] += 1
        task_keys[(record.dt, record.tag, record.ids, record.series_number)] += 1
        task_times.append(record.dt)
        empty_task_tags += not bool(record.tag)
        for key, value, index in (("tag", record.tag, by_tag), ("ids", record.ids, by_ids),
                                  ("series", record.series_number, by_series)):
            if value:
                index[value].append(record)
    tasks = {"id_duplicates": excess(task_ids), "exact_business_row_duplicates": excess(task_keys),
             "time": bounds(task_times), "empty_tags": empty_task_tags, "distinct_tags": len(by_tag)}

    wh_ids, wh_keys = Counter(), Counter()
    wh_times, nearest_deltas = [], []
    methods, candidate_methods, wh_missing = Counter(), Counter(), Counter()
    matched_raw = ambiguous = after_rows = after_raw = 0
    recent = []
    for row in source.rows("Warehouse"):
        at = dt(row["Dt"])
        wh_ids[int(row["Id"])] += 1
        wh_keys[(at, normalize_value(row["Tag"]), normalize_value(row["Ids"]), normalize_value(row["SeriesNumber"]))] += 1
        wh_times.append(at)
        for key in ("Tag", "Ids", "SeriesNumber"):
            wh_missing[key] += not bool(normalize_value(row[key]))
        pool = {}
        for index, key in ((by_tag, "Tag"), (by_ids, "Ids"), (by_series, "SeriesNumber")):
            for record in index.get(normalize_value(row[key]), ()):
                pool[record.row_id] = record
        if at is None:
            methods["MISSING_TIME"] += 1
            continue
        identity = resolve_warehouse_identity(row["Tag"], row["Ids"], row["SeriesNumber"], at, pool.values(), 24)
        methods[identity.primary_method] += 1
        candidate_methods[identity.candidates[0].method if identity.candidates else "NONE"] += 1
        ambiguous += identity.series_ambiguous
        nearest = None
        for candidate in identity.candidates:
            times = raw_by_tag.get(candidate.tag, [])
            lo, hi = bisect.bisect_left(times, at - timedelta(hours=24)), bisect.bisect_right(times, at + timedelta(hours=24))
            if hi > lo:
                insert = bisect.bisect_left(times, at, lo, hi)
                possible = [times[i] for i in (insert - 1, insert) if lo <= i < hi]
                nearest = min(possible, key=lambda x: abs((x - at).total_seconds()))
                break
        matched_raw += nearest is not None
        if nearest is not None:
            nearest_deltas.append(abs((nearest - at).total_seconds()))
        if at >= cutoff:
            after_rows += 1
            after_raw += nearest is not None
            recent.append({"warehouse_id": int(row["Id"]), "warehouse_time": at.isoformat(),
                           "task_id": identity.primary_task.row_id if identity.primary_task else None,
                           "task_match_method": identity.primary_method,
                           "nearest_raw_source_time": nearest.isoformat() if nearest else None})
    warehouse = {"id_duplicates": excess(wh_ids), "exact_business_row_duplicates": excess(wh_keys),
                 "time": bounds(wh_times), "empty": dict(wh_missing), "task_match_methods": dict(methods),
                 "physical_candidate_methods": dict(candidate_methods), "ambiguous_series_rows": ambiguous,
                 "rows_with_raw_candidate_within_24h": matched_raw,
                 "rows_without_raw_candidate_within_24h": source.inventory["Warehouse"]["rows"] - matched_raw,
                 "nearest_raw_absolute_lag_seconds": bounds_seconds(nearest_deltas),
                 "rows_since_hotfix": after_rows, "raw_candidates_since_hotfix": after_raw,
                 "recent_candidate_examples": sorted(recent, key=lambda x: x["warehouse_time"])[-5:]}

    skud_source_ids, directions = Counter(), Counter()
    skud_times = []
    for row in source.rows("RusGuardLogs"):
        if row["ExternalId2"]:
            skud_source_ids[normalize_value(row["ExternalId2"])] += 1
        directions[row["Direction"] or "<empty>"] += 1
        skud_times.append(dt(row["CreatedAt"]))
    skud_source = {"external_id_duplicates": excess(skud_source_ids), "time": bounds(skud_times),
                   "direction_values": dict(directions)}

    link_results, link_groups = {}, {}
    for table, id_column in (("KPP_EventVideoLinks", "VideoEventId"), ("KPP_EventSkudLinks", "SkudExternalId")):
        pairs, assignment = Counter(), defaultdict(set)
        times, count_values = [], Counter()
        missing_keys = missing_skud = 0
        for row in source.rows(table):
            group, target = normalize_value(row["PassageGroupKey"]), normalize_value(row[id_column])
            missing_keys += not group or not target
            pairs[(group, target)] += 1
            assignment[target].add(group)
            times.append(dt(row["LinkedAt"]))
            if table == "KPP_EventVideoLinks":
                count_values[row["ReelCount"] or "<empty>"] += 1
            else:
                missing_skud += target not in skud_source_ids
        groups = set().union(*assignment.values()) if assignment else set()
        link_groups[table] = groups
        result = {"pair_duplicates": excess(pairs), "source_ids_assigned_to_multiple_groups":
                  sum(len(groups) > 1 for groups in assignment.values()),
                  "missing_keys": missing_keys, "distinct_passage_groups": len(groups), "time": bounds(times)}
        if table == "KPP_EventVideoLinks":
            result["reel_count_values"] = dict(count_values)
        else:
            result["links_missing_rusguard_source"] = missing_skud
        link_results[table] = result
    sessions = list(source.rows("KPP_ActiveRfidSessions"))
    return {"cutoff_naive_sql_time": cutoff.isoformat(), "inventory": source.inventory, "raw_rfid": raw,
            "tasks_1c": tasks, "warehouse": warehouse, "rusguard": skud_source, "links": link_results,
            "groups_with_both_video_and_skud_links": len(link_groups["KPP_EventVideoLinks"] & link_groups["KPP_EventSkudLinks"]),
            "active_session_rows": len(sessions),
            "unavailable_evidence": ["KPP_ReelEvents", "ReelTransitions (split across archive volumes)",
                                     "Aggregator cursor/state", "physical SQLite spool SENT", "WEB response"],
            "interpretation": "Candidate identity/time matches do not prove a unique accepted physical passage. "
                              "IsProcessed is a legacy flag, not evidence of the Aggregator cursor. "
                              "This export has no product-type field to distinguish reels from coils."}


def bounds_seconds(values):
    return {"min": min(values), "max": max(values)} if values else {"min": None, "max": None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--hotfix-at", default="2026-10-06T18:54:00", help="Naive SQL server time; no timezone conversion")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = audit(args.folder, datetime.fromisoformat(args.hotfix_at))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("CSV_AUDIT_OK", len(result["inventory"]), "complete tables")


if __name__ == "__main__":
    main()
