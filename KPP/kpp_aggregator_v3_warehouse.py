#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Production wrapper for kpp_aggregator_v3 with Warehouse-only reconciliation.

Keeps the v3.4 RFID pipeline intact, but additionally treats dbo.Warehouse as an
independent physical source:
- if a Warehouse row matches an RFID/KPP event by full tag within +/-24h, enrich it;
- if no KPP event exists, create a WAREHOUSE_ONLY reel event;
- process Warehouse rows incrementally with a durable runtime cursor.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

from kpp_aggregator_v3 import Aggregator as BaseAggregator
from kpp_aggregator_v3 import Config, log


class Aggregator(BaseAggregator):
    WAREHOUSE_BATCH_SIZE = int(os.getenv("KPP_WAREHOUSE_RECONCILE_BATCH", "2000"))
    WAREHOUSE_RECONCILE_SEC = float(os.getenv("KPP_WAREHOUSE_RECONCILE_SEC", "15"))
    WAREHOUSE_STATE_KEY = "LAST_WAREHOUSE_ID_V3_4_1"

    def __init__(self) -> None:
        super().__init__()
        self.last_warehouse_reconcile = datetime.min

    @staticmethod
    def _normalize_tag(tag: object) -> str:
        return str(tag or "").strip().upper()

    def _find_task(self, cur, tag: str, dt: datetime) -> Optional[Tuple[int, datetime, str]]:
        start = dt - timedelta(hours=Config.TASK_WINDOW_HOURS)
        end = dt + timedelta(hours=Config.TASK_WINDOW_HOURS)
        cur.execute(
            f"""
SELECT TOP(1) Id,Dt,Ids
FROM {Config.TASK_TABLE}
WHERE UPPER(LTRIM(RTRIM(Tag)))=? AND Dt BETWEEN ? AND ?
ORDER BY ABS(DATEDIFF(SECOND,Dt,?)), Id DESC;
""",
            tag,
            start,
            end,
            dt,
        )
        row = cur.fetchone()
        if not row:
            return None
        return int(row[0]), row[1], str(row[2] or "")

    def _find_kpp_event(self, cur, tag: str, dt: datetime, warehouse_id: int) -> Optional[int]:
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
ORDER BY ABS(DATEDIFF(SECOND,FirstSeen,?)), EventId DESC;
""",
            tag,
            warehouse_id,
            start,
            end,
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
        task: Optional[Tuple[int, datetime, str]],
    ) -> None:
        task_id = task[0] if task else None
        task_dt = task[1] if task else None
        task_doc = task[2] if task else None
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
        task: Optional[Tuple[int, datetime, str]],
    ) -> None:
        event_key = hashlib.md5(f"WAREHOUSE_ONLY|{warehouse_id}|{tag}".encode("utf-8")).hexdigest()
        epc = tag[:24]
        tid = tag[24:] or None
        task_id = task[0] if task else None
        task_dt = task[1] if task else None
        task_doc = task[2] if task else None
        match_type = "FULL_TAG_BOTH" if task else "FULL_TAG_WAREHOUSE"
        evidence = json.dumps(
            {
                "processing_version": "3.4.1-warehouse",
                "source": "WAREHOUSE_ONLY",
                "warehouse": {
                    "id": warehouse_id,
                    "dt": warehouse_dt.isoformat(),
                    "series": series_number,
                    "doc_ids": warehouse_doc_ids,
                },
                "task_1c_id": task_id,
                "warnings": ["WAREHOUSE_ONLY_NO_RFID"],
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
        'WAREHOUSE_ONLY_NO_RFID',?,0,0,SYSDATETIME(),
        1,'REEL',?,'WAREHOUSE_TIME','3.4.1-warehouse',
        SYSDATETIME(),SYSDATETIME()
    );
END;
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
            evidence,
            match_type,
        )

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
            for row in rows:
                warehouse_id = int(row[0])
                max_id = max(max_id, warehouse_id)
                warehouse_dt = row[1]
                tag = self._normalize_tag(row[2])
                warehouse_doc_ids = str(row[3] or "")
                series_number = str(row[4] or "")
                if warehouse_dt is None or len(tag) < 24:
                    invalid += 1
                    continue

                task = self._find_task(cur, tag, warehouse_dt)
                event_id = self._find_kpp_event(cur, tag, warehouse_dt, warehouse_id)
                if event_id is not None:
                    self._enrich_existing_event(
                        cur, event_id, warehouse_id, warehouse_dt, warehouse_doc_ids, task
                    )
                    enriched += 1
                else:
                    self._insert_warehouse_only(
                        cur,
                        warehouse_id,
                        warehouse_dt,
                        tag,
                        warehouse_doc_ids,
                        series_number,
                        task,
                    )
                    warehouse_only += 1

            self.state_set(conn, self.WAREHOUSE_STATE_KEY, str(max_id))
            conn.commit()
            log.info(
                "WAREHOUSE COMMIT rows=%s enriched=%s warehouse_only=%s invalid=%s cursor=%s",
                len(rows), enriched, warehouse_only, invalid, max_id,
            )
            return len(rows)
        except Exception:
            conn.rollback()
            log.exception("Warehouse reconciliation rolled back")
            raise
        finally:
            conn.close()

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
