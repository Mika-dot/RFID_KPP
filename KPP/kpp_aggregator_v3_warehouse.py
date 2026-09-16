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
    normalize_tag,
    normalize_value,
    resolve_warehouse_identity,
)


class Aggregator(BaseAggregator):
    WAREHOUSE_BATCH_SIZE = int(os.getenv("KPP_WAREHOUSE_RECONCILE_BATCH", "2000"))
    WAREHOUSE_RECONCILE_SEC = float(os.getenv("KPP_WAREHOUSE_RECONCILE_SEC", "15"))
    WAREHOUSE_RECHECK_SEC = float(os.getenv("KPP_WAREHOUSE_RECHECK_SEC", "60"))
    WAREHOUSE_RECHECK_HOURS = float(os.getenv("KPP_WAREHOUSE_RECHECK_HOURS", "168"))
    WAREHOUSE_RECHECK_BATCH = int(os.getenv("KPP_WAREHOUSE_RECHECK_BATCH", "500"))
    # New cursor intentionally replays Warehouse from Id=0 once. Older builds
    # advanced their cursor past nullable-Tag rows after marking them invalid.
    WAREHOUSE_STATE_KEY = "LAST_WAREHOUSE_ID_V3_4_5_RECHECK"

    def __init__(self) -> None:
        super().__init__()
        self.last_warehouse_reconcile = datetime.min
        self.last_warehouse_recheck = datetime.min

    def bootstrap(self) -> None:
        super().bootstrap()
        with self.connect() as conn:
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
        start = dt - timedelta(hours=Config.TASK_WINDOW_HOURS)
        end = dt + timedelta(hours=Config.TASK_WINDOW_HOURS)
        cur.execute(
            f"""
SELECT TOP(1) EventId
FROM {Config.EVENT_TABLE}
WHERE UPPER(LTRIM(RTRIM(SourceTag)))=?
  AND ISNULL(SessionCloseReason,'')<>'WAREHOUSE_ONLY'
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
            dt,
        )
        row = cur.fetchone()
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
        linked = self._find_existing_linked_event(cur, warehouse_id, warehouse_dt)
        if linked is not None:
            event_id, linked_tag = linked
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
                    break

        if event_id is not None:
            task = (
                event_candidate.task if event_candidate is not None else None
            ) or identity.primary_task
            match_method = (
                event_candidate.method
                if event_candidate is not None
                else identity.primary_method
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
            identity.primary_task,
            match_method,
            link_status,
        )
        return "warehouse_only", identity.series_ambiguous

    def reconcile_warehouse(self, force: bool = False) -> int:
        now = datetime.now()
        if not force and (now - self.last_warehouse_reconcile).total_seconds() < self.WAREHOUSE_RECONCILE_SEC:
            return 0
        self.last_warehouse_reconcile = now

        conn = self.connect()
        try:
            saved = self.state_get(conn, self.WAREHOUSE_STATE_KEY)
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
