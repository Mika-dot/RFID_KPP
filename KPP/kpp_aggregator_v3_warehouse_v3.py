#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Warehouse reconciliation v3.4.5.

Address Warehouse is an independent confirmation of an outbound reel passage.
If RFID missed the tag but Warehouse has the reel, the event is treated as OUT
for report/UI purposes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta

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

    def maybe_log_status(self, conn, reads: int, closed: int, invalid: int) -> None:
        """Cheap status probe; never scan the full raw RFID history."""
        now = datetime.now()
        if (now - self.last_status_at).total_seconds() < Config.STATUS_SEC:
            return
        self.last_status_at = now
        cur = conn.cursor()
        cur.execute(
            f"""
SELECT TOP(1) Id, COALESCE(SourceReaderTime,RecordTime)
FROM {Config.RFID_TABLE}
ORDER BY Id DESC;
"""
        )
        row = cur.fetchone()
        source_max = int(row[0] or 0) if row else 0
        source_time = row[1] if row else None
        lag_rows = max(0, source_max - self.last_rfid_id)
        source_age = "нет данных" if source_time is None else f"{(now-source_time).total_seconds():.1f}с"
        last_event_age = "нет" if self.last_event_at is None else f"{(now-self.last_event_at).total_seconds():.1f}с"
        log.info(
            "STATUS cursor=%s source_max=%s lag_rows=%s source_time_age=%s batch_reads=%s batch_closed=%s invalid=%s active=%s total_reads=%s total_closed=%s reels=%s unknown_rfid=%s last_session_age=%s",
            self.last_rfid_id, source_max, lag_rows, source_age, reads, closed, invalid,
            len(self.sessionizer.active), self.total_reads, self.total_closed,
            self.total_reels, self.total_unknown, last_event_age,
        )

    def process_once(self) -> int:
        """Commit business state atomically; telemetry after COMMIT is best-effort.

        A status query is not part of business processing. If it fails after the
        SQL commit, RAM must NOT be restored to the pre-commit snapshot because
        the durable cursor/session checkpoint has already advanced.
        """
        self.load_registries()
        conn = self.connect()
        active_snapshot = self.snapshot_active()
        counters_snapshot = (
            self.total_reads,
            self.total_closed,
            self.total_reels,
            self.total_unknown,
            self.last_event_at,
        )
        reads = []
        closed = []
        row_errors = []
        batch_max_id = self.last_rfid_id
        try:
            reads, batch_max_id, row_errors = self.fetch_reads(conn)
            now = datetime.now()
            for read in reads:
                closed.extend(self.sessionizer.process(read))
            closed.extend(self.close_ready_sessions(reads, len(reads) + len(row_errors), now))
            self.persist_sessions(conn, closed)
            self.record_processing_errors(conn, row_errors)
            self.checkpoint(conn, batch_max_id)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            finally:
                self.restore_active_snapshot(active_snapshot)
                (
                    self.total_reads,
                    self.total_closed,
                    self.total_reels,
                    self.total_unknown,
                    self.last_event_at,
                ) = counters_snapshot
                conn.close()
            log.exception("Пакет откатан; cursor и in-memory sessions восстановлены")
            raise

        # From this point the DB commit is irreversible. Advance the in-memory
        # cursor before any non-critical status/telemetry I/O.
        self.last_rfid_id = batch_max_id
        self.total_reads += len(reads)
        self.total_closed += len(closed)
        if reads or closed or row_errors:
            log.info(
                "ПАКЕТ COMMIT reads=%s invalid=%s closed=%s active=%s cursor=%s",
                len(reads), len(row_errors), len(closed), len(self.sessionizer.active), self.last_rfid_id,
            )
        try:
            self.maybe_log_status(conn, len(reads), len(closed), len(row_errors))
        except Exception:
            # Never reinterpret an already committed batch as failed merely
            # because a diagnostic SELECT/logging operation failed.
            log.exception("STATUS query failed after committed batch; business state remains committed")
        finally:
            conn.close()
        return len(reads) + len(row_errors)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="обработать один пакет и выйти")
    args = parser.parse_args()
    Aggregator().run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
