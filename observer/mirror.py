"""Bounded SELECT-only synchronization of business metadata into a local mirror."""
from __future__ import annotations

import os
from datetime import datetime

from observer.collector import table


EVENT_FIELDS = ("EventId", "FirstSeen", "LastSeen", "UpdatedAt", "SourceTag",
                "RfidReadCount", "RfidDirection", "FinalDirection", "ConfidencePct",
                "TransportMode", "WarehouseId", "WarehouseDt", "Task1CId", "NeedRecheck",
                "ObjectType", "SessionCloseReason", "WarningFlags", "VideoMatched",
                "SkudMatched", "ReelClassification", "IsReel")
IDENTITY_FIELDS = ("Id", "Dt", "Tag", "Ids", "SeriesNumber")


def json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def sync_metadata(mirror, connection=None, batch_size=500):
    if type(batch_size) is not int or not 1 <= batch_size <= 2000:
        raise ValueError("MirrorBatchInvalid")
    if connection is None:
        import pyodbc
        destination = os.environ.get("PERIMETER_OBSERVER_SQL") or os.environ["PERIMETER_HA_SQL"]
        connection = pyodbc.connect(destination, timeout=3, autocommit=True, readonly=True)
    result = {}
    try:
        connection.timeout = 3
        connection.execute("SET LOCK_TIMEOUT 2000")
        for stream, env, default in (
            ("events", "OBSERVER_EVENTS_TABLE", "dbo.KPP_ReelEvents"),
            ("warehouse", "OBSERVER_WAREHOUSE_TABLE", "dbo.Warehouse"),
            ("tasks", "OBSERVER_TASK_TABLE", "dbo.RfidTags"),
        ):
            source = table(os.getenv(env, default))
            saved = mirror.watermark(stream)
            # A restored/rolled-back primary must not make a newer local cache
            # appear current merely because the incremental SELECT is empty.
            # Deleted anchors also require operator reconciliation, not a reset.
            if saved:
                if stream == "events":
                    anchor = connection.execute(
                        f"SELECT EventId FROM {source} WHERE EventId=? AND UpdatedAt>=?",
                        int(saved[1]), saved[0]).fetchone()
                else:
                    anchor = connection.execute(
                        f"SELECT Id FROM {source} WHERE Id=?", int(saved)).fetchone()
                if not anchor:
                    raise RuntimeError("MirrorSourceHistoryChanged:" + stream)
            if stream == "events":
                stamp, source_id = saved or ["1900-01-01T00:00:00", 0]
                fields = EVENT_FIELDS
                sql = f"""SELECT TOP ({batch_size}) {','.join(fields)} FROM {source}
                    WHERE FirstSeen>=DATEADD(day,-?,SYSDATETIME())
                    AND (UpdatedAt>? OR (UpdatedAt=? AND EventId>?))
                    ORDER BY UpdatedAt,EventId"""
                rows = connection.execute(sql, mirror.retention_days, stamp, stamp, source_id).fetchall()
                position = [json_value(rows[-1][3]), int(rows[-1][0])] if rows else [stamp, source_id]
            else:
                source_id = int(saved or 0)
                fields = IDENTITY_FIELDS
                rows = connection.execute(f"""SELECT TOP ({batch_size}) {','.join(fields)} FROM {source}
                    WHERE Id>? AND Dt>=DATEADD(day,-?,SYSDATETIME()) ORDER BY Id""",
                    source_id, mirror.retention_days).fetchall()
                position = int(rows[-1][0]) if rows else source_id
            prepared = [(int(row[0]), row[1], {key: json_value(value) for key, value in zip(fields, row)}) for row in rows]
            mirror.commit_batch(stream, prepared, position, caught_up=len(rows) < batch_size)
            result[stream] = len(prepared)
        mirror.maintenance()
        return result
    finally:
        connection.close()
