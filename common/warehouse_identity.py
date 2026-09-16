#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure Warehouse -> 1C identity resolution shared by aggregator and WEB.

Warehouse rows always carry ``Ids`` and ``SeriesNumber``. ``Tag`` is optional.
The physical RFID identity is resolved in this strict order:

1. Warehouse.Tag (direct physical identity);
2. Warehouse.Ids -> RfidTags.Ids -> RfidTags.Tag;
3. Warehouse.SeriesNumber -> RfidTags.SeriesNumber -> RfidTags.Tag, but only
   when the series maps to exactly one distinct non-empty tag in the window.

Empty values never form an identity and therefore can never match each other.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional, Tuple


MATCH_TAG = "TAG"
MATCH_IDS = "IDS"
MATCH_SERIES = "SERIES"
MATCH_NONE = "NONE"
MATCH_AMBIGUOUS_SERIES = "AMBIGUOUS_SERIES"


def normalize_value(value: object) -> str:
    return str(value or "").strip().upper()


def normalize_tag(value: object) -> str:
    return normalize_value(value)


@dataclass(frozen=True)
class IdentityRecord:
    row_id: int
    dt: datetime
    tag: str
    ids: str = ""
    series_number: str = ""

    @classmethod
    def from_values(
        cls,
        row_id: object,
        dt: datetime,
        tag: object,
        ids: object = "",
        series_number: object = "",
    ) -> "IdentityRecord":
        return cls(
            row_id=int(row_id),
            dt=dt,
            tag=normalize_tag(tag),
            ids=normalize_value(ids),
            series_number=normalize_value(series_number),
        )


@dataclass(frozen=True)
class IdentityCandidate:
    tag: str
    method: str
    task: Optional[IdentityRecord] = None


@dataclass(frozen=True)
class IdentityResolution:
    candidates: Tuple[IdentityCandidate, ...]
    primary_task: Optional[IdentityRecord]
    primary_method: str
    series_ambiguous: bool = False

    @property
    def preferred_tag(self) -> str:
        return self.candidates[0].tag if self.candidates else ""


def _nearest(records: Iterable[IdentityRecord], dt: datetime) -> Optional[IdentityRecord]:
    values = list(records)
    if not values:
        return None
    return min(values, key=lambda row: (abs((row.dt - dt).total_seconds()), -row.row_id))


def resolve_warehouse_identity(
    warehouse_tag: object,
    warehouse_ids: object,
    warehouse_series: object,
    warehouse_dt: datetime,
    task_rows: Iterable[IdentityRecord],
    window_hours: float = 24,
) -> IdentityResolution:
    """Resolve candidate physical tags without ever equating empty values."""
    tag = normalize_tag(warehouse_tag)
    ids = normalize_value(warehouse_ids)
    series = normalize_value(warehouse_series)
    max_delta = timedelta(hours=window_hours)
    rows = [
        row
        for row in task_rows
        if row.dt is not None
        and row.tag
        and abs(row.dt - warehouse_dt) <= max_delta
    ]

    tag_task = _nearest((row for row in rows if tag and row.tag == tag), warehouse_dt)
    ids_task = _nearest((row for row in rows if ids and row.ids == ids), warehouse_dt)

    series_rows = [row for row in rows if series and row.series_number == series]
    series_tags = {row.tag for row in series_rows}
    series_ambiguous = len(series_tags) > 1
    series_task = None if series_ambiguous else _nearest(series_rows, warehouse_dt)

    primary_task: Optional[IdentityRecord]
    primary_method: str
    if tag_task is not None:
        primary_task, primary_method = tag_task, MATCH_TAG
    elif ids_task is not None:
        primary_task, primary_method = ids_task, MATCH_IDS
    elif series_task is not None:
        primary_task, primary_method = series_task, MATCH_SERIES
    else:
        primary_task = None
        primary_method = MATCH_AMBIGUOUS_SERIES if series_ambiguous else MATCH_NONE

    candidates = []

    def add_candidate(candidate_tag: str, method: str, task: Optional[IdentityRecord]) -> None:
        if not candidate_tag:
            return
        for idx, existing in enumerate(candidates):
            if existing.tag == candidate_tag:
                # Keep the higher-priority method, but do not lose a 1C row that
                # was recovered through Ids/SeriesNumber.
                if existing.task is None and task is not None:
                    candidates[idx] = IdentityCandidate(existing.tag, existing.method, task)
                return
        candidates.append(IdentityCandidate(candidate_tag, method, task))

    add_candidate(tag, MATCH_TAG, tag_task)
    add_candidate(ids_task.tag if ids_task else "", MATCH_IDS, ids_task)
    add_candidate(series_task.tag if series_task else "", MATCH_SERIES, series_task)

    return IdentityResolution(tuple(candidates), primary_task, primary_method, series_ambiguous)
