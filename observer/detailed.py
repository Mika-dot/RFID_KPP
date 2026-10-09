"""Bounded source statistics. Only aggregate numbers leave this module."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import math


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def percentile(values, fraction):
    values = sorted(v for v in (number(x) for x in values) if v is not None)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lo = int(position)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (position - lo)


def delta(later, earlier):
    try:
        later = later if isinstance(later, datetime) else datetime.fromisoformat(str(later))
        earlier = earlier if isinstance(earlier, datetime) else datetime.fromisoformat(str(earlier))
        seconds = (later - earlier).total_seconds()
        return seconds if seconds >= 0 else None
    except (ValueError, TypeError):
        return None


def distribution(out, name, values, limits=(25, 50, 75, 90)):
    values = [v for v in (number(x) for x in values) if v is not None]
    if not values:
        return
    for index in range(len(limits) + 1):
        lower = limits[index - 1] if index else -math.inf
        upper = limits[index] if index < len(limits) else math.inf
        out[name + "_bucket_" + str(index)] = sum(lower <= v < upper for v in values)


def ambiguous_series_count(warehouses, tasks, hours):
    """Sliding windows avoid an unbounded recent-warehouse × task product."""
    candidates, wanted = defaultdict(list), defaultdict(list)
    norm = lambda value: str(value or "").strip().upper()
    for tag, series, stamp in tasks:
        if norm(tag):
            candidates[norm(series)].append((stamp, norm(tag)))
    for row in warehouses:
        wanted[norm(row["SeriesNumber"])].append(row["Dt"])
    result = 0
    window = timedelta(hours=hours)
    for series, stamps in wanted.items():
        rows = sorted(candidates[series])
        left = right = 0
        tags = Counter()
        for stamp in sorted(stamps):
            while right < len(rows) and rows[right][0] <= stamp + window:
                tags[rows[right][1]] += 1
                right += 1
            while left < right and rows[left][0] < stamp - window:
                tag = rows[left][1]
                tags[tag] -= 1
                if tags[tag] == 0:
                    del tags[tag]
                left += 1
            result += len(tags) > 1
    return result


def summarize(stream, rows):
    out = {}
    if stream == "rfid":
        tags = defaultdict(list)
        for row in rows:
            if row.get("EPC"):
                tags[(row["EPC"], row.get("TID"))].append(row)
        if rows:
            out["rfid_approximate_time_ratio"] = sum("APPROXIMATE" in str(r.get("TimeQuality", "")) for r in rows) / len(rows)
        if tags:
            out["rfid_reads_per_tag"] = sum(map(len, tags.values())) / len(tags)
        gaps, transitions = [], Counter()
        for reads in tags.values():
            # Database source sequence (Id), not a reconstructed wall-clock sort.
            reads.sort(key=lambda r: r["Id"])
            for previous, current in zip(reads, reads[1:]):
                gap = delta(current.get("RecordTime"), previous.get("RecordTime"))
                if gap is not None:
                    gaps.append(gap)
                a, b = previous.get("Antenna"), current.get("Antenna")
                if a in (1, 2, 3, 4) and b in (1, 2, 3, 4):
                    transitions[(a, b)] += 1
        if tags:
            for a in range(1, 5):
                for b in range(1, 5):
                    out[f"rfid_antenna_transition_matrix_{a}_{b}"] = transitions[(a, b)]
        for key, values, fraction in (
            ("rfid_rssi_median", [r.get("RSSI") for r in rows], .5),
            ("rfid_rssi_p95", [r.get("RSSI") for r in rows], .95),
            ("rfid_inter_read_gap", gaps, .95),
            ("rfid_delivery_latency_p95", [delta(r.get("ReceivedAt"), r.get("RecordTime")) for r in rows], .95)):
            value = percentile(values, fraction)
            if value is not None:
                out[key] = value
    elif stream == "video":
        if rows:
            cameras = Counter(r.get("ToCamera") for r in rows)
            out["video_camera_asymmetry"] = abs(cameras[0] - cameras[1]) / len(rows)
            out["video_grouped_reel_count"] = sum(number(r.get("ReelCount")) or 0 for r in rows)
        value = percentile([r.get("TimeDiffSec") for r in rows], .95)
        if value is not None:
            out["video_transition_time"] = value
    elif stream == "skud":
        if rows:
            out["skud_missing_identity_ratio"] = sum(not r.get("HasIdentity") for r in rows) / len(rows)
            # Gate names/card/person identifiers never enter the result.
            devices = Counter(r.get("PersonControlDeviceName") for r in rows)
            counts = sorted(devices.values(), reverse=True)
            for index, count in enumerate(counts[:16]):
                out[f"skud_device_distribution_rank_{index}"] = count
            out["skud_device_distribution_other"] = sum(counts[16:])
            for direction in ("IN", "OUT", "UNKNOWN"):
                out["skud_direction_" + direction.lower() + "_5min"] = sum(r.get("Direction") == direction for r in rows)
        value = percentile([delta(r.get("ReceivedAt"), r.get("CreatedAt")) for r in rows], .95)
        if value is not None:
            out["skud_source_delay"] = value
    elif stream == "events":
        physical = [r for r in rows if (number(r.get("RfidReadCount")) or 0) > 0]
        if physical:
            out["unknown_rfid_ratio"] = sum(not r.get("IsReel") for r in physical) / len(physical)
            out["direction_conflict_ratio"] = sum("CONFLICT" in str(r.get("WarningFlags", "")) for r in physical) / len(physical)
        groups = Counter(r.get("PassageGroupKey") for r in physical if r.get("IsReel") and r.get("PassageGroupKey"))
        if groups:
            out["passage_group_reel_count"] = sum(groups.values()) / len(groups)
        if rows:
            out["warehouse_superseded_ratio"] = sum(r.get("SessionCloseReason") == "SUPERSEDED_BY_KPP" for r in rows) / len(rows)
        distribution(out, "confidence_distribution", [r.get("ConfidencePct") for r in physical])
        for key, values in (
            ("rfid_session_duration", [delta(r.get("LastSeen"), r.get("FirstSeen")) for r in physical]),
            ("warehouse_registration_delay_p95", [delta(r.get("WarehouseDt"), r.get("LastSeen")) for r in physical if r.get("WarehouseId")]),
            ("processing_latency", [delta(r.get("CompletedAt"), r.get("LastSeen")) for r in physical]),
            ("video_match_delay", [abs(v) for v in (number(r.get("VideoTimeDiffSec")) for r in physical if r.get("VideoMatched")) if v is not None])):
            value = percentile(values, .95)
            if value is not None:
                out[key] = value
        for source, column in (("video", "VideoTimeDiffSec"), ("skud", "SkudTimeDiffSec")):
            value = percentile([abs(v) for v in (number(r.get(column)) for r in physical) if v is not None], .95)
            if value is not None:
                out["source_time_deltas_" + source] = value
    return out
