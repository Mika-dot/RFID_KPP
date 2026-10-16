"""Keep passage direction separate from an address-warehouse outbound fact.

Warehouse can fill a missing direction, but cannot override an observed IN or
resolve an explicit fusion conflict. This policy is shared by writes and Web
projections; applying it to a projection never updates the database.
"""
from __future__ import annotations

from typing import Any, Mapping


def effective_direction_sql(prefix: str = "") -> str:
    if prefix not in {"", "e."}:
        raise ValueError("UnsupportedDirectionAlias")
    direction = f"UPPER(LTRIM(RTRIM(ISNULL({prefix}FinalDirection,'UNKNOWN'))))"
    consensus = f"UPPER(LTRIM(RTRIM(ISNULL({prefix}ConsensusCode,''))))"
    return (f"(CASE WHEN {prefix}WarehouseId IS NOT NULL AND {direction} NOT IN ('IN','OUT') "
            f"AND {consensus}<>'CONFLICT' THEN 'OUT' ELSE {direction} END)")


def warehouse_direction_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    direction = str(record.get("FinalDirection") or "UNKNOWN").strip().upper()
    consensus = str(record.get("ConsensusCode") or "").strip().upper()
    warnings = list(dict.fromkeys(
        flag.strip() for flag in str(record.get("WarningFlags") or "").split("|")
        if flag.strip()
    ))
    # These are derived claims, rather than source evidence. Recompute them so
    # an old OUT flag cannot survive a later IN/conflicting passage decision.
    derived = {"OUT_CONFIRMED_BY_WAREHOUSE", "WAREHOUSE_DIRECTION_CONFLICT",
               "WAREHOUSE_DIRECTION_INFERRED"}
    inferred_before = "WAREHOUSE_DIRECTION_INFERRED" in warnings
    warnings = [flag for flag in warnings if flag not in derived]
    if "WAREHOUSE_CONFIRMED" not in warnings:
        warnings.append("WAREHOUSE_CONFIRMED")

    confidence = record.get("ConfidencePct")
    conflict = direction == "IN" or (direction not in {"IN", "OUT"} and consensus == "CONFLICT")
    inferred = direction not in {"IN", "OUT"} and not conflict
    if inferred:
        direction, confidence = "OUT", 100
    if conflict:
        warnings.append("WAREHOUSE_DIRECTION_CONFLICT")
    elif direction == "OUT":
        warnings.append("OUT_CONFIRMED_BY_WAREHOUSE")
        if inferred or inferred_before:
            warnings.append("WAREHOUSE_DIRECTION_INFERRED")

    return {
        "FinalDirection": direction,
        "ConfidencePct": confidence,
        "WarningFlags": " | ".join(warnings),
        "WarehouseDirection": "OUT",
        "WarehouseDirectionConflict": conflict,
    }


def project_warehouse_direction(record: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(record)
    if result.get("WarehouseId") is not None:
        result.update(warehouse_direction_fields(result))
    return result
