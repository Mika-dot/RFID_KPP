#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Warehouse reconciliation v3.4.5.

Address Warehouse is an independent confirmation of an outbound reel passage.
If RFID missed the tag but Warehouse has the reel, the event is treated as OUT
for report/UI purposes.
"""
from __future__ import annotations

import argparse
from datetime import timedelta

from kpp_aggregator_v3_warehouse import Aggregator as WarehouseAggregator
from kpp_aggregator_v3_warehouse import Config, log


class Aggregator(WarehouseAggregator):
    def bootstrap(self) -> None:
        super().bootstrap()

        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f"""
UPDATE {Config.EVENT_TABLE}
SET FinalDirection='OUT',
    ConfidencePct=CASE WHEN ISNULL(ConfidencePct,0)<100 THEN 100 ELSE ConfidencePct END,
    WarningFlags=CASE
        WHEN CHARINDEX('OUT_CONFIRMED_BY_WAREHOUSE',ISNULL(WarningFlags,''))>0 THEN WarningFlags
        WHEN ISNULL(WarningFlags,'')='' THEN 'OUT_CONFIRMED_BY_WAREHOUSE'
        ELSE CONCAT(WarningFlags,' | OUT_CONFIRMED_BY_WAREHOUSE')
    END,
    ProcessingVersion='3.4.5-warehouse-recheck',
    UpdatedAt=SYSDATETIME()
WHERE IsReel=1
  AND WarehouseId IS NOT NULL
  AND (FinalDirection IS NULL OR FinalDirection='UNKNOWN');
"""
            )
            repaired = int(cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if repaired:
            log.info(
                "WAREHOUSE direction repair: OUT confirmed for %s historical events",
                repaired,
            )

    def _find_existing_linked_event(self, cur, warehouse_id, warehouse_dt):
        """Reuse any real RFID event already linked to this Warehouse row.

        Do not require IsReel=1 here: older/early processing may have persisted
        the raw RFID passage as UNKNOWN_RFID before 1C/Warehouse evidence became
        available. Warehouse confirmation is exactly the evidence that can
        safely promote such an RFID event to a reel.
        """
        cur.execute(
            f"""
SELECT TOP(1) EventId,SourceTag
FROM {Config.EVENT_TABLE}
WHERE WarehouseId=?
  AND ISNULL(SessionCloseReason,'')<>'WAREHOUSE_ONLY'
  AND ISNULL(RfidReadCount,0)>0
ORDER BY ABS(DATEDIFF(SECOND,FirstSeen,?)),EventId DESC;
""",
            warehouse_id,
            warehouse_dt,
        )
        row = cur.fetchone()
        if not row:
            return None
        return int(row[0]), str(row[1] or "").strip().upper()

    def _find_kpp_event(self, cur, tag, dt, warehouse_id):
        """Find a physical RFID passage, including UNKNOWN_RFID events.

        The base implementation used ``IsReel=1``. That loses an important
        distinction in the report: RFID may have physically read a reel while
        the aggregator failed to classify it before Warehouse/1C evidence
        arrived. Requiring a positive raw RFID count prevents Warehouse from
        attaching to synthetic/non-RFID rows while allowing the later evidence
        to promote the real passage through ``_enrich_existing_event``.
        """
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
            dt,
        )
        row = cur.fetchone()
        return int(row[0]) if row else None

    def _enrich_existing_event(
        self,
        cur,
        event_id,
        warehouse_id,
        warehouse_dt,
        warehouse_doc_ids,
        series_number,
        task,
        match_method,
    ) -> None:
        super()._enrich_existing_event(
            cur,
            event_id,
            warehouse_id,
            warehouse_dt,
            warehouse_doc_ids,
            series_number,
            task,
            match_method,
        )
        cur.execute(
            f"""
UPDATE {Config.EVENT_TABLE}
SET FinalDirection=CASE
        WHEN FinalDirection IS NULL OR FinalDirection='UNKNOWN' THEN 'OUT'
        ELSE FinalDirection
    END,
    ConfidencePct=CASE
        WHEN FinalDirection IS NULL OR FinalDirection='UNKNOWN' THEN 100
        ELSE ConfidencePct
    END,
    WarningFlags=CASE
        WHEN CHARINDEX('OUT_CONFIRMED_BY_WAREHOUSE',ISNULL(WarningFlags,''))>0 THEN WarningFlags
        WHEN ISNULL(WarningFlags,'')='' THEN 'OUT_CONFIRMED_BY_WAREHOUSE'
        ELSE CONCAT(WarningFlags,' | OUT_CONFIRMED_BY_WAREHOUSE')
    END,
    ProcessingVersion='3.4.5-warehouse-recheck',
    UpdatedAt=SYSDATETIME()
WHERE EventId=?;
""",
            event_id,
        )

    def _insert_warehouse_only(
        self,
        cur,
        warehouse_id,
        warehouse_dt,
        tag,
        warehouse_doc_ids,
        series_number,
        task,
        match_method,
        link_status,
    ) -> None:
        super()._insert_warehouse_only(
            cur,
            warehouse_id,
            warehouse_dt,
            tag,
            warehouse_doc_ids,
            series_number,
            task,
            match_method,
            link_status,
        )
        cur.execute(
            f"""
UPDATE {Config.EVENT_TABLE}
SET FinalDirection='OUT',
    ConfidencePct=100,
    WarningFlags=CASE
        WHEN CHARINDEX('OUT_CONFIRMED_BY_WAREHOUSE',ISNULL(WarningFlags,''))>0 THEN WarningFlags
        WHEN ISNULL(WarningFlags,'')='' THEN 'OUT_CONFIRMED_BY_WAREHOUSE'
        ELSE CONCAT(WarningFlags,' | OUT_CONFIRMED_BY_WAREHOUSE')
    END,
    ProcessingVersion='3.4.5-warehouse-recheck',
    UpdatedAt=SYSDATETIME()
WHERE WarehouseId=? AND SessionCloseReason='WAREHOUSE_ONLY' AND IsReel=1;
""",
            warehouse_id,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="обработать один пакет и выйти")
    args = parser.parse_args()
    Aggregator().run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
