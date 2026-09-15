#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Warehouse reconciliation v3.4.2.

Extends the v3.4.1 Warehouse reconciler so an address-warehouse confirmation is
also a factual OUT passage for reporting/UI purposes. RFID remains the preferred
checkpoint source; Warehouse is the independent fallback source when RFID missed.
"""
from __future__ import annotations

import argparse

from kpp_aggregator_v3_warehouse import Aggregator as WarehouseAggregator
from kpp_aggregator_v3_warehouse import Config, log


class Aggregator(WarehouseAggregator):
    def bootstrap(self) -> None:
        super().bootstrap()
        # Historical repair: if the address warehouse confirmed the reel, the
        # physical movement from workshop to warehouse happened even when RFID
        # did not determine a direction. Keep explicit RFID directions intact.
        with self.connect() as conn:
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
    UpdatedAt=SYSDATETIME()
WHERE IsReel=1
  AND WarehouseId IS NOT NULL
  AND (FinalDirection IS NULL OR FinalDirection='UNKNOWN');
"""
            )
            repaired = int(cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0)
            conn.commit()
        if repaired:
            log.info("WAREHOUSE direction repair: OUT confirmed for %s historical events", repaired)

    def _enrich_existing_event(
        self,
        cur,
        event_id,
        warehouse_id,
        warehouse_dt,
        warehouse_doc_ids,
        task,
    ) -> None:
        super()._enrich_existing_event(
            cur,
            event_id,
            warehouse_id,
            warehouse_dt,
            warehouse_doc_ids,
            task,
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
    ProcessingVersion='3.4.2-warehouse-union',
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
    ) -> None:
        super()._insert_warehouse_only(
            cur,
            warehouse_id,
            warehouse_dt,
            tag,
            warehouse_doc_ids,
            series_number,
            task,
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
    ProcessingVersion='3.4.2-warehouse-union',
    UpdatedAt=SYSDATETIME()
WHERE WarehouseId=? AND SessionCloseReason='WAREHOUSE_ONLY';
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
