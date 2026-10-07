#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Semantic business-flow health helpers for Perimeter.

Transport/process health is not enough for a physical checkpoint: a process can
stay alive while one data source silently stops producing events. Keep the
classification pure so production adapters and regression tests use the same
rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class RfidFlowAssessment:
    status: str
    detail: str
    rfid_age_seconds: Optional[float]
    video_age_seconds: Optional[float]
    warehouse_age_seconds: Optional[float]
    latch_fault: bool = False
    clear_latch: bool = False


def source_marker(value: Optional[datetime]) -> str:
    if value is None:
        return "<none>"
    return value.isoformat(timespec="milliseconds")


def _age_seconds(now: datetime, value: Optional[datetime]) -> Optional[float]:
    if value is None:
        return None
    try:
        return max(0.0, (now - value).total_seconds())
    except TypeError:
        # SQL DATETIME2 is normally naive. Keep this robust if a driver returns
        # timezone-aware values unexpectedly.
        return max(
            0.0,
            (now.replace(tzinfo=None) - value.replace(tzinfo=None)).total_seconds(),
        )


def _after(value: Optional[datetime], baseline: Optional[datetime]) -> bool:
    if value is None:
        return False
    if baseline is None:
        return True
    try:
        return value > baseline
    except TypeError:
        return value.replace(tzinfo=None) > baseline.replace(tzinfo=None)


def assess_rfid_flow(
    *,
    now: datetime,
    rfid_at: Optional[datetime],
    video_at: Optional[datetime],
    warehouse_at: Optional[datetime],
    video_recent_events: int,
    warehouse_recent_events: int,
    stall_seconds: float,
    min_video_events: int = 2,
    fault_latched: bool = False,
    latched_rfid_marker: str = "",
    last_fault_detail: str = "",
    video_history_complete: bool = False,
) -> RfidFlowAssessment:
    """Classify RFID business flow using independent Perimeter evidence.

    Zero RFID reads is *not* automatically a fault because the checkpoint can be
    idle. A stale RFID source becomes a fault when video/Warehouse prove that
    physical/business activity continued. Once confirmed, the fault is latched
    until a *fresh* new raw RFID timestamp appears; a delayed backlog row after
    restart is not proof that the live reader recovered.
    """
    stall_seconds = max(1.0, float(stall_seconds))
    min_video_events = max(1, int(min_video_events))
    video_recent_events = max(0, int(video_recent_events))
    warehouse_recent_events = max(0, int(warehouse_recent_events))

    rfid_age = _age_seconds(now, rfid_at)
    video_age = _age_seconds(now, video_at)
    warehouse_age = _age_seconds(now, warehouse_at)
    marker = source_marker(rfid_at)
    rfid_fresh = rfid_age is not None and rfid_age <= stall_seconds

    # Window counts can include the very passage that produced the last RFID
    # row. They only prove silence if independent activity happened afterwards.
    video_after_read = _after(video_at, rfid_at)
    warehouse_after_read = _after(warehouse_at, rfid_at)
    counted_video_events = video_recent_events
    video_recent_events = video_recent_events if video_after_read else 0
    warehouse_recent_events = warehouse_recent_events if warehouse_after_read else 0

    if fault_latched:
        if (
            rfid_at is not None
            and marker != str(latched_rfid_marker or "")
            and rfid_fresh
        ):
            return RfidFlowAssessment(
                "ok",
                "recovered_new_fresh_rfid_read",
                rfid_age,
                video_age,
                warehouse_age,
                clear_latch=True,
            )
        # A complete MAX(video timestamp) snapshot can contradict the old
        # detector's proof, rather than merely letting evidence age out. Keep
        # the persistent latch and restart history intact, expose a warning,
        # and do not keep cycling hosts for video predating the same RFID row.
        # Unknown/corrupt metadata, missing history, a changed/backlog marker,
        # or any later physical passage still fail closed.
        if (video_history_complete is True and rfid_at is not None
                and marker == str(latched_rfid_marker or "")
                and video_at is not None and not video_after_read
                and counted_video_events == 0
                and last_fault_detail in {"rfid_stale_while_video_active",
                                          "rfid_stale_while_video_and_warehouse_active"}):
            return RfidFlowAssessment(
                "degraded", "rfid_historical_activity_evidence_contradicted",
                rfid_age, video_age, warehouse_age,
            )
        return RfidFlowAssessment(
            "unavailable",
            "rfid_business_flow_fault_latched",
            rfid_age,
            video_age,
            warehouse_age,
        )

    rfid_stale = not rfid_fresh
    if not rfid_stale:
        return RfidFlowAssessment(
            "ok",
            "rfid_source_fresh",
            rfid_age,
            video_age,
            warehouse_age,
        )

    # One video transition plus a Warehouse row is strong cross-source evidence
    # that traffic exists while RFID remains silent.
    if video_recent_events > 0 and warehouse_recent_events > 0:
        return RfidFlowAssessment(
            "unavailable",
            "rfid_stale_while_video_and_warehouse_active",
            rfid_age,
            video_age,
            warehouse_age,
            latch_fault=True,
        )

    # Multiple physical video passages are independently sufficient. Requiring
    # more than one avoids restarting the reader for a single genuinely
    # untagged/missed reel.
    if video_recent_events >= min_video_events:
        return RfidFlowAssessment(
            "unavailable",
            "rfid_stale_while_video_active",
            rfid_age,
            video_age,
            warehouse_age,
            latch_fault=True,
        )

    # Partial/delayed evidence keeps business_flow degraded. The health adapter
    # exposes this as a warning while operational dependencies remain ready;
    # it cannot by itself trigger a hardware restart or HA failover.
    if warehouse_recent_events > 0 or video_recent_events > 0:
        return RfidFlowAssessment(
            "degraded",
            "rfid_stale_with_partial_activity_evidence",
            rfid_age,
            video_age,
            warehouse_age,
        )

    return RfidFlowAssessment(
        "ok",
        "checkpoint_idle_no_activity_evidence",
        rfid_age,
        video_age,
        warehouse_age,
    )
