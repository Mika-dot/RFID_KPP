#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Production wrapper for kpp_aggregator_v3 with Warehouse-only reconciliation.

Keeps the v3.4 RFID pipeline intact, but additionally treats dbo.Warehouse as an
independent physical source:
- match Warehouse to RFID/KPP by Tag, then Ids, then unambiguous SeriesNumber;
- Tag is optional while Ids and SeriesNumber are required;
- if no KPP event exists, create a WAREHOUSE_ONLY reel event;
- process Warehouse rows incrementally with a durable runtime cursor.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from kpp_aggregator_v3 import Aggregator as BaseAggregator
from kpp_aggregator_v3 import Config, log
from common.warehouse_identity import (
    IdentityRecord,
    IdentityResolution,
    MATCH_AMBIGUOUS_SERIES,
    MATCH_NONE,
    normalize_tag,
    normalize_value,
    resolve_warehouse_identity,
)
from common.adaptive_windows import AdaptiveWindowModel, TravelObservation


class Aggregator(BaseAggregator):
    WAREHOUSE_BATCH_SIZE = int(os.getenv("KPP_WAREHOUSE_RECONCILE_BATCH", "2000"))
    WAREHOUSE_RECONCILE_SEC = float(os.getenv("KPP_WAREHOUSE_RECONCILE_SEC", "15"))
    WAREHOUSE_RECHECK_SEC = float(os.getenv("KPP_WAREHOUSE_RECHECK_SEC", "60"))
    WAREHOUSE_RECHECK_HOURS = float(os.getenv("KPP_WAREHOUSE_RECHECK_HOURS", "168"))
    WAREHOUSE_RECHECK_BATCH = int(os.getenv("KPP_WAREHOUSE_RECHECK_BATCH", "500"))
    # Adaptive KPP -> Warehouse windows are opt-in until a replay/shadow run
    # has accepted the learned profile. The legacy symmetric window remains
    # the safe default for an untrained/cold model.
    ADAPTIVE_WINDOWS_ENABLED = os.getenv("KPP_ADAPTIVE_WINDOWS_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    ADAPTIVE_WINDOWS_MODE = os.getenv("KPP_ADAPTIVE_WINDOWS_MODE", "shadow").strip().lower()
    ADAPTIVE_WINDOW_ACCEPTED_PROFILE = os.getenv("KPP_ADAPTIVE_WINDOW_ACCEPTED_PROFILE", "")
    ADAPTIVE_WINDOW_MIN_SAMPLES = int(os.getenv("KPP_ADAPTIVE_WINDOW_MIN_SAMPLES", "8"))
    ADAPTIVE_WINDOW_DEFAULT_LOWER_SEC = float(os.getenv("KPP_ADAPTIVE_WINDOW_DEFAULT_LOWER_SEC", "0"))
    ADAPTIVE_WINDOW_DEFAULT_UPPER_SEC = float(os.getenv("KPP_ADAPTIVE_WINDOW_DEFAULT_UPPER_SEC", "86400"))
    ADAPTIVE_WINDOW_HARD_MAX_SEC = float(os.getenv("KPP_ADAPTIVE_WINDOW_HARD_MAX_SEC", "604800"))
    ADAPTIVE_WINDOW_MARGIN_SEC = float(os.getenv("KPP_ADAPTIVE_WINDOW_MARGIN_SEC", "30"))
    ADAPTIVE_WINDOW_LOOKBACK_DAYS = int(os.getenv("KPP_ADAPTIVE_WINDOW_LOOKBACK_DAYS", "180"))
    ADAPTIVE_WINDOW_REFRESH_SEC = float(os.getenv("KPP_ADAPTIVE_WINDOW_REFRESH_SEC", "900"))
    ADAPTIVE_WINDOW_STATE_KEY = "KPP_ADAPTIVE_TRAVEL_MODEL_V1"
    # New cursor intentionally replays Warehouse from Id=0 once. Older builds
    # advanced their cursor past nullable-Tag rows after marking them invalid.
    WAREHOUSE_STATE_KEY = "LAST_WAREHOUSE_ID_V3_4_5_RECHECK"

    def __init__(self) -> None:
        super().__init__()
        self.last_warehouse_reconcile = datetime.min
        self.last_warehouse_recheck = datetime.min
        self.last_adaptive_refresh = datetime.min
        if self.ADAPTIVE_WINDOWS_MODE not in {"off", "shadow", "active"}:
            raise ValueError("AdaptiveWindowModeInvalid")
        self.accepted_windows = None
        self._adaptive_link_evidence = {}
        self.adaptive_windows = self._new_adaptive_model()
        if self.ADAPTIVE_WINDOWS_ENABLED and self.ADAPTIVE_WINDOWS_MODE == "active":
            from deploy.ha.tune_adaptive_windows import load_accepted_profile
            self.accepted_windows = load_accepted_profile(self.ADAPTIVE_WINDOW_ACCEPTED_PROFILE)

    def _new_adaptive_model(self):
        return AdaptiveWindowModel(
            default_lower_sec=self.ADAPTIVE_WINDOW_DEFAULT_LOWER_SEC,
            default_upper_sec=self.ADAPTIVE_WINDOW_DEFAULT_UPPER_SEC,
            hard_max_sec=self.ADAPTIVE_WINDOW_HARD_MAX_SEC,
            margin_sec=self.ADAPTIVE_WINDOW_MARGIN_SEC,
            min_samples=self.ADAPTIVE_WINDOW_MIN_SAMPLES,
        )

    def bootstrap(self) -> None:
        super().bootstrap()
        with self.connect() as conn:
            self._load_adaptive_model(conn)
            cur = conn.cursor()
            cur.execute(
                f"""
UPDATE {Config.EVENT_TABLE}
SET NeedRecheck=1,
    NextRecheckAt=COALESCE(NextRecheckAt,SYSDATETIME()),
    FinalizedAt=NULL,
    UpdatedAt=SYSDATETIME()
WHERE SessionCloseReason='WAREHOUSE_ONLY'
  AND IsReel=1
  AND WarehouseDt>=DATEADD(hour,-?,SYSDATETIME());

UPDATE {Config.EVENT_TABLE}
SET NeedRecheck=0,
    NextRecheckAt=NULL,
    FinalizedAt=COALESCE(FinalizedAt,SYSDATETIME()),
    UpdatedAt=SYSDATETIME()
WHERE SessionCloseReason='WAREHOUSE_ONLY'
  AND IsReel=1
  AND WarehouseDt<DATEADD(hour,-?,SYSDATETIME());
""",
                self.WAREHOUSE_RECHECK_HOURS,
                self.WAREHOUSE_RECHECK_HOURS,
            )
            conn.commit()

    def _load_adaptive_model(self, conn) -> None:
        """Load the last shadow model; it never authorizes active matching."""
        raw = self.state_get(conn, self.ADAPTIVE_WINDOW_STATE_KEY)
        if not raw:
            return
        try:
            self.adaptive_windows = AdaptiveWindowModel.loads(raw)
        except Exception as exc:
            log.warning("Adaptive travel model ignored: %s", type(exc).__name__)

    def _refresh_adaptive_model(self, conn, force: bool = False) -> bool:
        """Learn only from persisted physical KPP events already linked to Warehouse.

        This query is read-only apart from persisting the compact model in the
        runtime state table. Warehouse-only rows and direction conflicts never
        become training labels. Repeated scans are idempotent by event/warehouse
        identifiers held by ``AdaptiveWindowModel``.
        """
        if not self.ADAPTIVE_WINDOWS_ENABLED or self.ADAPTIVE_WINDOWS_MODE == "off":
            return False
        now = datetime.now()
        if not force and (now - self.last_adaptive_refresh).total_seconds() < self.ADAPTIVE_WINDOW_REFRESH_SEC:
            return False
        self.last_adaptive_refresh = now
        cur = conn.cursor()
        lookback_days = max(1, int(self.ADAPTIVE_WINDOW_LOOKBACK_DAYS))
        cur.execute(
            f"""
SELECT TOP ({max(1, int(self.WAREHOUSE_RECHECK_BATCH * 10))})
       EventId,WarehouseId,FirstSeen,WarehouseDt,TransportMode,FinalDirection,WarningFlags
FROM {Config.EVENT_TABLE}
WHERE WarehouseId IS NOT NULL
  AND ISNULL(RfidReadCount,0)>0
  AND ISNULL(SessionCloseReason,'')<>'WAREHOUSE_ONLY'
  AND FirstSeen IS NOT NULL
  AND WarehouseDt IS NOT NULL
  AND WarehouseDt>=DATEADD(day,-?,SYSDATETIME())
  AND JSON_VALUE(CASE WHEN ISJSON(EvidenceJson)=1 THEN EvidenceJson ELSE '{{}}' END,'$.warehouse.adaptive_match') IS NULL
  AND JSON_VALUE(CASE WHEN ISJSON(EvidenceJson)=1 THEN EvidenceJson ELSE '{{}}' END,'$.warehouse.match_method') IN ('TAG','IDS')
ORDER BY WarehouseDt DESC,EventId DESC;
""",
            lookback_days,
        )
        candidate = self._new_adaptive_model()
        for row in reversed(cur.fetchall()):
            direction = str(row[5] or "").strip().upper()
            warnings = str(row[6] or "").upper()
            # A warehouse outbound fact must not train on an explicitly
            # conflicting physical direction. UNKNOWN is retained only when
            # Warehouse is the sole positive evidence and the RFID event is
            # still a real read (RfidReadCount>0).
            if direction == "IN" or "DIRECTION_CONFLICT" in warnings:
                continue
            candidate.observe(
                TravelObservation(
                    kpp_at=row[2],
                    warehouse_at=row[3],
                    transport=row[4] or "UNKNOWN",
                    event_id=int(row[0]),
                    warehouse_id=int(row[1]),
                    confirmed=True,
                )
            )
        changed = candidate.dumps() != self.adaptive_windows.dumps()
        if changed:
            self.state_set(conn, self.ADAPTIVE_WINDOW_STATE_KEY, candidate.dumps())
            self.adaptive_windows = candidate
        return changed

    def _warehouse_search_interval(self, warehouse_dt: datetime, transport: str = "UNKNOWN") -> tuple[datetime, datetime]:
        """Return a safe search interval for a possible KPP event."""
        if not self.ADAPTIVE_WINDOWS_ENABLED or self.ADAPTIVE_WINDOWS_MODE != "active":
            span = timedelta(hours=Config.TASK_WINDOW_HOURS)
            return warehouse_dt - span, warehouse_dt + span
        model = self.accepted_windows
        if model is None:
            raise ValueError("ActiveAdaptiveProfileRequired")
        start, end, bounds = model.backward_interval(warehouse_dt, transport)
        if bounds.confidence == "cold_start":
            # Do not narrow a cold model and silently hide historical events.
            span = timedelta(hours=Config.TASK_WINDOW_HOURS)
            return warehouse_dt - span, warehouse_dt + span
        return start, end

    def _retrospective_evidence(self, cur, tag, warehouse_dt):
        """Audit raw inputs backwards; never manufacture an RFID passage."""
        if not tag or not self.ADAPTIVE_WINDOWS_ENABLED or self.ADAPTIVE_WINDOWS_MODE == "off":
            return None
        bounds = self.adaptive_windows.bounds_for(warehouse_dt)
        expected_start, expected_end = bounds.backward_interval(warehouse_dt)
        recovery_start = warehouse_dt - timedelta(seconds=self.ADAPTIVE_WINDOW_HARD_MAX_SEC)
        legacy_start = warehouse_dt - timedelta(hours=Config.TASK_WINDOW_HOURS)
        legacy_end = warehouse_dt + timedelta(hours=Config.TASK_WINDOW_HOURS)
        cur.execute(f"""
SELECT TOP(200) Id,COALESCE(SourceReaderTime,RecordTime),Antenna,RSSI
FROM {Config.RFID_TABLE}
WHERE UPPER(LTRIM(RTRIM(EPC)))=? AND UPPER(LTRIM(RTRIM(ISNULL(TID,''))))=?
  AND COALESCE(SourceReaderTime,RecordTime) BETWEEN ? AND ?
ORDER BY ABS(DATEDIFF(SECOND,COALESCE(SourceReaderTime,RecordTime),?)),Id;
""", tag[:24], tag[24:], recovery_start, warehouse_dt,
            warehouse_dt - timedelta(seconds=bounds.center_sec))
        rows = cur.fetchall()
        from common.retrospective import session_candidates
        candidates = session_candidates(tag, warehouse_dt, rows, expected_start, expected_end,
                                        legacy_start, legacy_end, Config.OUTER_ANTENNAS, Config.INNER_ANTENNAS)
        return {
            "version": 1, "status": "RAW_CANDIDATES" if rows else "NO_RAW_CANDIDATE",
            "expected_window": [expected_start.isoformat(), expected_end.isoformat()],
            "recovery_window": [recovery_start.isoformat(), warehouse_dt.isoformat()],
            "model_bucket": bounds.bucket, "model_samples": bounds.sample_count,
            "model_status": bounds.confidence, "page_limit": 200,
            "raw_candidates_in_page": len(rows),
            "outside_legacy_window": sum(not legacy_start <= row[1] <= legacy_end for row in rows),
            "raw_candidates": [{"id": int(row[0]), "at": row[1].isoformat(),
                                "antenna": row[2], "rssi": row[3]} for row in rows],
            "session_candidates": candidates,
            "association_status": "REVIEW_REQUIRED" if candidates else "NO_SESSION_CANDIDATE",
            "identity_basis": "exact_epc_tid",
            "learning_eligible": False, "physical_passage_confirmed": False,
        }

    def _load_task_candidates(
        self,
        cur,
        tag: str,
        doc_ids: str,
        series_number: str,
        dt: datetime,
    ) -> List[IdentityRecord]:
        start = dt - timedelta(hours=Config.TASK_WINDOW_HOURS)
        end = dt + timedelta(hours=Config.TASK_WINDOW_HOURS)
        clauses = []
        params: List[object] = [start, end]
        if tag:
            clauses.append("UPPER(LTRIM(RTRIM(Tag)))=?")
            params.append(tag)
        if doc_ids:
            try:
                canonical_ids = str(uuid.UUID(doc_ids))
            except (ValueError, AttributeError):
                canonical_ids = ""
            if canonical_ids:
                # dbo.RfidTags.Ids is UNIQUEIDENTIFIER. String functions on it
                # fail in SQL Server; conversion of the parameter remains
                # sargable against an index on Ids.
                clauses.append("Ids=CONVERT(uniqueidentifier, ?)")
                params.append(canonical_ids)
        if series_number:
            clauses.append("UPPER(LTRIM(RTRIM(SeriesNumber)))=?")
            params.append(series_number)
        if not clauses:
            return []
        cur.execute(
            f"""
SELECT Id,Dt,Tag,Ids,SeriesNumber
FROM {Config.TASK_TABLE}
WHERE Dt BETWEEN ? AND ?
  AND NULLIF(LTRIM(RTRIM(ISNULL(Tag,''))),'') IS NOT NULL
  AND ({' OR '.join(clauses)});
""",
            params,
        )
        return [
            IdentityRecord.from_values(row[0], row[1], row[2], row[3], row[4])
            for row in cur.fetchall()
            if row[1] is not None
        ]

    def _resolve_identity(
        self,
        cur,
        tag: str,
        doc_ids: str,
        series_number: str,
        dt: datetime,
    ) -> IdentityResolution:
        rows = self._load_task_candidates(cur, tag, doc_ids, series_number, dt)
        return resolve_warehouse_identity(
            tag,
            doc_ids,
            series_number,
            dt,
            rows,
            Config.TASK_WINDOW_HOURS,
        )

    def _find_existing_linked_event(
        self, cur, warehouse_id: int, warehouse_dt: datetime
    ) -> Optional[tuple[int, str]]:
        cur.execute(
            f"""
SELECT TOP(1) EventId,SourceTag
FROM {Config.EVENT_TABLE}
WHERE WarehouseId=?
  AND ISNULL(SessionCloseReason,'')<>'WAREHOUSE_ONLY'
  AND ISNULL(RfidReadCount,0)>0
  AND IsReel=1
ORDER BY ABS(DATEDIFF(SECOND,FirstSeen,?)),EventId DESC;
""",
            warehouse_id,
            warehouse_dt,
        )
        row = cur.fetchone()
        return (int(row[0]), normalize_tag(row[1])) if row else None

    def _find_kpp_event(self, cur, tag: str, dt: datetime, warehouse_id: int) -> Optional[int]:
        if not tag:
            return None
        start, end = self._warehouse_search_interval(dt)
        target_dt = dt
        profile_evidence = None
        if self.ADAPTIVE_WINDOWS_ENABLED and self.ADAPTIVE_WINDOWS_MODE == "active":
            bounds = self.accepted_windows.bounds_for(dt)
            target_dt = dt - timedelta(seconds=bounds.center_sec)
            if bounds.confidence != "cold_start":
                profile_evidence = {"adaptive_match": True, "learning_eligible": False,
                                    "window_start": start.isoformat(), "window_end": end.isoformat(),
                                    "bucket": bounds.bucket, "sample_count": bounds.sample_count,
                                    "profile_status": bounds.confidence}
        cur.execute(
            f"""
SELECT TOP(1) EventId
FROM {Config.EVENT_TABLE}
WHERE UPPER(LTRIM(RTRIM(SourceTag)))=?
  AND ISNULL(SessionCloseReason,'')<>'WAREHOUSE_ONLY'
  AND ISNULL(RfidReadCount,0)>0
  AND (WarehouseId IS NULL OR WarehouseId=?)
  AND FirstSeen BETWEEN ? AND ?
ORDER BY CASE WHEN WarehouseId=? THEN 0 ELSE 1 END,
         ABS(DATEDIFF(SECOND,FirstSeen,?)), EventId DESC;
""",
            tag,
            warehouse_id,
            start,
            end,
            warehouse_id,
            target_dt,
        )
        row = cur.fetchone()
        if row is not None and profile_evidence is not None:
            if not hasattr(self, "_adaptive_link_evidence"):
                self._adaptive_link_evidence = {}
            self._adaptive_link_evidence[int(row[0])] = profile_evidence
        return int(row[0]) if row else None

    def _enrich_existing_event(
        self,
        cur,
        event_id: int,
        warehouse_id: int,
        warehouse_dt: datetime,
        warehouse_doc_ids: str,
        series_number: str,
        task: Optional[IdentityRecord],
        match_method: str,
    ) -> None:
        task_id = task.row_id if task else None
        task_dt = task.dt if task else None
        task_doc = task.ids if task else None
        link_status = f"MATCH_{match_method}"
        warehouse_evidence = json.dumps(
            {
                "id": warehouse_id,
                "dt": warehouse_dt.isoformat(),
                "doc_ids": warehouse_doc_ids,
                "series_number": series_number,
                "match_method": match_method,
                "link_status": link_status,
                **getattr(self, "_adaptive_link_evidence", {}).pop(event_id, {}),
            },
            ensure_ascii=False,
        )
        cur.execute(
            f"""
UPDATE {Config.EVENT_TABLE}
SET WarehouseId=?, WarehouseDt=?, WarehouseDocIds=?,
    Task1CId=COALESCE(Task1CId,?),
    Task1CDt=COALESCE(Task1CDt,?),
    Task1CDocIds=COALESCE(Task1CDocIds,?),
    IsReel=1,
    ObjectType='REEL',
    ReelClassification=CASE WHEN COALESCE(Task1CId,?) IS NOT NULL THEN 'FULL_TAG_BOTH' ELSE 'FULL_TAG_WAREHOUSE' END,
    TaskMatchType=CASE WHEN COALESCE(Task1CId,?) IS NOT NULL THEN 'FULL_TAG_BOTH' ELSE 'FULL_TAG_WAREHOUSE' END,
    WarningFlags=CASE
        WHEN CHARINDEX('WAREHOUSE_CONFIRMED',ISNULL(WarningFlags,''))>0 THEN WarningFlags
        WHEN ISNULL(WarningFlags,'')='' THEN 'WAREHOUSE_CONFIRMED'
        ELSE CONCAT(WarningFlags,' | WAREHOUSE_CONFIRMED')
    END,
    EvidenceJson=JSON_MODIFY(
        CASE WHEN ISJSON(EvidenceJson)=1 THEN EvidenceJson ELSE '{{}}' END,
        '$.warehouse',JSON_QUERY(?)),
    UpdatedAt=SYSDATETIME()
WHERE EventId=?;
""",
            warehouse_id,
            warehouse_dt,
            warehouse_doc_ids,
            task_id,
            task_dt,
            task_doc,
            task_id,
            task_id,
            warehouse_evidence,
            event_id,
        )

    def _supersede_warehouse_only(self, cur, warehouse_id: int, event_id: int) -> None:
        cur.execute(
            f"""
UPDATE {Config.EVENT_TABLE}
SET IsReel=0,
    ObjectType='SUPERSEDED',
    ReelClassification='SUPERSEDED_BY_KPP',
    NeedRecheck=0,
    NextRecheckAt=NULL,
    FinalizedAt=COALESCE(FinalizedAt,SYSDATETIME()),
    WarningFlags=CASE
        WHEN CHARINDEX('SUPERSEDED_BY_KPP',ISNULL(WarningFlags,''))>0 THEN WarningFlags
        WHEN ISNULL(WarningFlags,'')='' THEN 'SUPERSEDED_BY_KPP'
        ELSE CONCAT(WarningFlags,' | SUPERSEDED_BY_KPP')
    END,
    EvidenceJson=JSON_MODIFY(
        CASE WHEN ISJSON(EvidenceJson)=1 THEN EvidenceJson ELSE '{{}}' END,
        '$.superseded_by_event_id',?),
    ProcessingVersion='3.4.5-warehouse-recheck',
    UpdatedAt=SYSDATETIME()
WHERE WarehouseId=?
  AND SessionCloseReason='WAREHOUSE_ONLY'
  AND EventId<>?
  AND IsReel=1;
""",
            event_id,
            warehouse_id,
            event_id,
        )

    def _insert_warehouse_only(
        self,
        cur,
        warehouse_id: int,
        warehouse_dt: datetime,
        tag: str,
        warehouse_doc_ids: str,
        series_number: str,
        task: Optional[IdentityRecord],
        match_method: str,
        link_status: str,
    ) -> None:
        event_key = hashlib.md5(
            f"WAREHOUSE_ONLY|{warehouse_id}|{tag}|{warehouse_doc_ids}|{series_number}".encode("utf-8")
        ).hexdigest()
        epc = tag[:24]
        tid = tag[24:] or None
        task_id = task.row_id if task else None
        task_dt = task.dt if task else None
        task_doc = task.ids if task else None
        match_type = "WAREHOUSE_ONLY"
        evidence = json.dumps(
            {
                "processing_version": "3.4.5-warehouse-recheck",
                "source": "WAREHOUSE_ONLY",
                "warehouse": {
                    "id": warehouse_id,
                    "dt": warehouse_dt.isoformat(),
                    "series_number": series_number,
                    "doc_ids": warehouse_doc_ids,
                    "resolved_tag": tag,
                    "match_method": match_method,
                    "link_status": link_status,
                },
                "task_1c_id": task_id,
                "warnings": [
                    "WAREHOUSE_ONLY_NO_RFID",
                    *(["AMBIGUOUS_SERIES"] if link_status == MATCH_AMBIGUOUS_SERIES else []),
                ],
            },
            ensure_ascii=False,
        )
        cur.execute(
            f"""
IF NOT EXISTS (
    SELECT 1 FROM {Config.EVENT_TABLE}
    WHERE WarehouseId=? AND SessionCloseReason='WAREHOUSE_ONLY'
)
BEGIN
    INSERT INTO {Config.EVENT_TABLE}(
        EventKey,SourceTag,EPC,TID,
        Task1CId,Task1CDt,Task1CDocIds,TaskMatchType,
        WarehouseId,WarehouseDt,WarehouseDocIds,
        FirstSeen,LastSeen,CompletedAt,SessionCloseReason,
        RfidReadCount,DistinctAntennaCount,DistinctZoneCount,
        RfidDirection,RfidDirectionScore,
        VideoMatched,VideoScore,SkudMatched,SkudScore,
        FinalDirection,ConfidencePct,ConsensusCode,ScoreIn,ScoreOut,SourceCount,TransportMode,
        WarningFlags,EvidenceJson,NeedRecheck,RecheckCount,FinalizedAt,
        IsReel,ObjectType,ReelClassification,SourceTimeQuality,ProcessingVersion,
        CreatedAt,UpdatedAt
    ) VALUES (
        ?,?,?,?,
        ?,?,?,?,
        ?,?,?,
        ?,?,?, 'WAREHOUSE_ONLY',
        0,0,0,
        'UNKNOWN',0,
        0,0,0,0,
        'UNKNOWN',100,'SINGLE',0,0,1,'WAREHOUSE',
        ?,?,1,0,NULL,
        1,'REEL',?,'WAREHOUSE_TIME','3.4.5-warehouse-recheck',
        SYSDATETIME(),SYSDATETIME()
    );
END;
UPDATE {Config.EVENT_TABLE}
SET SourceTag=?,EPC=?,TID=?,
    Task1CId=COALESCE(Task1CId,?),
    Task1CDt=COALESCE(Task1CDt,?),
    Task1CDocIds=COALESCE(Task1CDocIds,?),
    EvidenceJson=?,
    NeedRecheck=1,
    NextRecheckAt=DATEADD(second,?,SYSDATETIME()),
    LastRecheckAt=SYSDATETIME(),
    RecheckCount=ISNULL(RecheckCount,0)+1,
    FinalizedAt=NULL,
    ProcessingVersion='3.4.5-warehouse-recheck',
    UpdatedAt=SYSDATETIME()
WHERE WarehouseId=? AND SessionCloseReason='WAREHOUSE_ONLY' AND IsReel=1;
""",
            warehouse_id,
            event_key,
            tag,
            epc,
            tid,
            task_id,
            task_dt,
            task_doc,
            match_type,
            warehouse_id,
            warehouse_dt,
            warehouse_doc_ids,
            warehouse_dt,
            warehouse_dt,
            warehouse_dt,
            "WAREHOUSE_ONLY_NO_RFID" + (" | AMBIGUOUS_SERIES" if link_status == MATCH_AMBIGUOUS_SERIES else ""),
            evidence,
            match_type,
            tag,
            epc,
            tid,
            task_id,
            task_dt,
            task_doc,
            evidence,
            int(self.WAREHOUSE_RECHECK_SEC),
            warehouse_id,
        )

    def _process_warehouse_row(self, cur, row) -> tuple[str, bool]:
        warehouse_id = int(row[0])
        warehouse_dt = row[1]
        tag = normalize_tag(row[2])
        warehouse_doc_ids = str(row[3] or "").strip()
        series_number = str(row[4] or "").strip()
        normalized_ids = normalize_value(warehouse_doc_ids)
        normalized_series = normalize_value(series_number)
        # Tag is deliberately optional. Ids and SeriesNumber are mandatory 1C
        # identity values on every valid Warehouse row.
        if warehouse_dt is None or not normalized_ids or not normalized_series:
            return "invalid", False

        identity = self._resolve_identity(
            cur,
            tag,
            normalized_ids,
            normalized_series,
            warehouse_dt,
        )

        event_id = None
        event_candidate = None
        event_tag = ""
        linked = self._find_existing_linked_event(cur, warehouse_id, warehouse_dt)
        if linked is not None:
            event_id, linked_tag = linked
            event_tag = linked_tag
            event_candidate = next(
                (candidate for candidate in identity.candidates if candidate.tag == linked_tag),
                None,
            )

        if event_id is None:
            for candidate in identity.candidates:
                event_id = self._find_kpp_event(
                    cur, candidate.tag, warehouse_dt, warehouse_id
                )
                if event_id is not None:
                    event_candidate = candidate
                    event_tag = candidate.tag
                    break

        if event_id is not None:
            task = identity.task_for_tag(event_tag)
            match_method = (
                event_candidate.method
                if event_candidate is not None
                else MATCH_NONE
            )
            self._enrich_existing_event(
                cur,
                event_id,
                warehouse_id,
                warehouse_dt,
                warehouse_doc_ids,
                series_number,
                task,
                match_method,
            )
            self._supersede_warehouse_only(cur, warehouse_id, event_id)
            return "enriched", identity.series_ambiguous

        match_method = (
            identity.candidates[0].method
            if identity.candidates
            else identity.primary_method
        )
        link_status = (
            MATCH_AMBIGUOUS_SERIES
            if identity.series_ambiguous and not identity.candidates
            else "WAREHOUSE_ONLY"
        )
        self._insert_warehouse_only(
            cur,
            warehouse_id,
            warehouse_dt,
            identity.preferred_tag,
            warehouse_doc_ids,
            series_number,
            identity.task_for_tag(identity.preferred_tag),
            match_method,
            link_status,
        )
        evidence = self._retrospective_evidence(cur, identity.preferred_tag, warehouse_dt)
        if evidence is not None:
            cur.execute(f"""UPDATE {Config.EVENT_TABLE}
SET EvidenceJson=JSON_MODIFY(CASE WHEN ISJSON(EvidenceJson)=1 THEN EvidenceJson ELSE '{{}}' END,
    '$.retrospective',JSON_QUERY(?)),UpdatedAt=SYSDATETIME()
WHERE WarehouseId=? AND SessionCloseReason='WAREHOUSE_ONLY' AND IsReel=1;""",
                        json.dumps(evidence, ensure_ascii=False), warehouse_id)
        return "warehouse_only", identity.series_ambiguous

    def reconcile_warehouse(self, force: bool = False) -> int:
        now = datetime.now()
        if not force and (now - self.last_warehouse_reconcile).total_seconds() < self.WAREHOUSE_RECONCILE_SEC:
            return 0
        self.last_warehouse_reconcile = now

        conn = self.connect()
        try:
            saved = self.state_get(conn, self.WAREHOUSE_STATE_KEY)
            self._refresh_adaptive_model(conn)
            last_id = int(saved or 0)
            cur = conn.cursor()
            cur.execute(
                f"""
SELECT TOP ({self.WAREHOUSE_BATCH_SIZE}) Id,Dt,Tag,Ids,SeriesNumber
FROM {Config.WAREHOUSE_TABLE}
WHERE Id>?
ORDER BY Id ASC;
""",
                last_id,
            )
            rows = cur.fetchall()
            if not rows:
                conn.commit()
                return 0

            max_id = last_id
            enriched = 0
            warehouse_only = 0
            invalid = 0
            ambiguous_series = 0
            for row in rows:
                warehouse_id = int(row[0])
                max_id = max(max_id, warehouse_id)
                outcome, ambiguous = self._process_warehouse_row(cur, row)
                if ambiguous:
                    ambiguous_series += 1
                if outcome == "invalid":
                    invalid += 1
                elif outcome == "enriched":
                    enriched += 1
                else:
                    warehouse_only += 1

            self.state_set(conn, self.WAREHOUSE_STATE_KEY, str(max_id))
            conn.commit()
            log.info(
                "WAREHOUSE COMMIT rows=%s enriched=%s warehouse_only=%s invalid=%s ambiguous_series=%s cursor=%s",
                len(rows), enriched, warehouse_only, invalid, ambiguous_series, max_id,
            )
            return len(rows)
        except Exception:
            conn.rollback()
            log.exception("Warehouse reconciliation rolled back")
            raise
        finally:
            conn.close()

    def recheck_warehouse_only(self, force: bool = False) -> int:
        now = datetime.now()
        if not force and (
            now - self.last_warehouse_recheck
        ).total_seconds() < self.WAREHOUSE_RECHECK_SEC:
            return 0
        self.last_warehouse_recheck = now

        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f"""
SELECT TOP ({self.WAREHOUSE_RECHECK_BATCH})
       w.Id,w.Dt,w.Tag,w.Ids,w.SeriesNumber
FROM {Config.EVENT_TABLE} e
JOIN {Config.WAREHOUSE_TABLE} w ON w.Id=e.WarehouseId
WHERE e.SessionCloseReason='WAREHOUSE_ONLY'
  AND e.IsReel=1
  AND e.NeedRecheck=1
  AND (e.NextRecheckAt IS NULL OR e.NextRecheckAt<=SYSDATETIME())
  AND w.Dt>=DATEADD(hour,-?,SYSDATETIME())
ORDER BY COALESCE(e.NextRecheckAt,e.CreatedAt),e.EventId;
""",
                self.WAREHOUSE_RECHECK_HOURS,
            )
            rows = cur.fetchall()
            enriched = 0
            for row in rows:
                outcome, _ambiguous = self._process_warehouse_row(cur, row)
                if outcome == "enriched":
                    enriched += 1

            cur.execute(
                f"""
UPDATE {Config.EVENT_TABLE}
SET NeedRecheck=0,
    NextRecheckAt=NULL,
    FinalizedAt=COALESCE(FinalizedAt,SYSDATETIME()),
    WarningFlags=CASE
        WHEN CHARINDEX('WAREHOUSE_RECHECK_EXPIRED',ISNULL(WarningFlags,''))>0 THEN WarningFlags
        WHEN ISNULL(WarningFlags,'')='' THEN 'WAREHOUSE_RECHECK_EXPIRED'
        ELSE CONCAT(WarningFlags,' | WAREHOUSE_RECHECK_EXPIRED')
    END,
    UpdatedAt=SYSDATETIME()
WHERE SessionCloseReason='WAREHOUSE_ONLY'
  AND IsReel=1
  AND NeedRecheck=1
  AND WarehouseDt<DATEADD(hour,-?,SYSDATETIME());
""",
                self.WAREHOUSE_RECHECK_HOURS,
            )
            conn.commit()
            if rows:
                log.info(
                    "WAREHOUSE RECHECK rows=%s enriched=%s pending=%s",
                    len(rows), enriched, len(rows) - enriched,
                )
            return len(rows)
        except Exception:
            conn.rollback()
            log.exception("Warehouse-only recheck rolled back")
            raise
        finally:
            conn.close()

    def recheck_pending(self) -> int:
        rfid_count = super().recheck_pending()
        return rfid_count + self.recheck_warehouse_only()

    def run(self, once: bool = False) -> None:
        self.bootstrap()
        # First pass also backfills historical Warehouse rows from cursor 0.
        self.reconcile_warehouse(force=True)
        while True:
            try:
                count = self.process_once()
                self.recheck_pending()
                wh_count = self.reconcile_warehouse()
                if once:
                    return
                if count >= Config.RFID_BATCH_SIZE or wh_count >= self.WAREHOUSE_BATCH_SIZE:
                    continue
                time.sleep(Config.POLL_SEC)
            except KeyboardInterrupt:
                log.warning("Остановка пользователем. Активные сессии уже durable, искусственно не закрываются.")
                return
            except Exception:
                if once:
                    raise
                time.sleep(max(2.0, Config.POLL_SEC))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="обработать один пакет и выйти")
    args = parser.parse_args()
    Aggregator().run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
