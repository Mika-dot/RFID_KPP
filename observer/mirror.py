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
RFID_FIELDS = ("Id", "RecordTime", "Antenna", "RSSI", "EPC", "TID",
               "ClientReadUuid", "ReceivedAt", "SourceReaderTime", "SourceSequence",
               "IngestBatchId", "TimeQuality")
VIDEO_FIELDS = ("Id", "Timestamp", "Direction", "FromCamera", "ToCamera",
                "TransportMode", "TimeDiffSec", "DetectionCount", "ClientEventUuid",
                "CapturedAt", "ProcessedAt", "ReceivedAt", "ReelCount", "SourceTrackIds", "TimeQuality")
SKUD_FIELDS = ("ExternalId2", "CreatedAt", "Direction", "ReceivedAt")
STREAMS = (
    ("events", "OBSERVER_EVENTS_TABLE", "dbo.KPP_ReelEvents", EVENT_FIELDS, "EventId", "FirstSeen"),
    ("warehouse", "OBSERVER_WAREHOUSE_TABLE", "dbo.Warehouse", IDENTITY_FIELDS, "Id", "Dt"),
    ("tasks", "OBSERVER_TASK_TABLE", "dbo.RfidTags", IDENTITY_FIELDS, "Id", "Dt"),
    ("rfid", "OBSERVER_RFID_TABLE", "dbo.RFID_Tags", RFID_FIELDS, "Id", "RecordTime"),
    ("video", "OBSERVER_VIDEO_TABLE", "dbo.ReelTransitions", VIDEO_FIELDS, "Id", "Timestamp"),
    ("skud", "OBSERVER_SKUD_TABLE", "dbo.RusGuardLogs", SKUD_FIELDS, "ExternalId2", "CreatedAt"),
)
REQUIRED_STREAMS = tuple(item[0] for item in STREAMS)


def json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def sync_metadata(mirror, connection=None, batch_size=500, task_connection=None):
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
        task_destination = os.getenv("PERIMETER_OBSERVER_TASK_SQL") or os.getenv("KPP_TASK_CONN_STR")
        if task_connection is None and task_destination:
            import pyodbc
            task_connection = pyodbc.connect(task_destination, timeout=3, autocommit=True, readonly=True)
        if task_connection is not None and task_connection is not connection:
            task_connection.timeout = 3
            task_connection.execute("SET LOCK_TIMEOUT 2000")
        for stream, env, default, fields, id_column, time_column in STREAMS:
            source_connection = (task_connection or connection) if stream in {"tasks", "warehouse"} else connection
            source = table(os.getenv(env, default))
            saved = mirror.watermark(stream)
            # A restored/rolled-back primary must not make a newer local cache
            # appear current merely because the incremental SELECT is empty.
            # Deleted anchors also require operator reconciliation, not a reset.
            if saved and (stream != "events" or int(saved[1]) > 0):
                if stream == "events":
                    anchor = source_connection.execute(
                        f"SELECT EventId FROM {source} WHERE EventId=? AND UpdatedAt>=?",
                        int(saved[1]), saved[0]).fetchone()
                else:
                    anchor = source_connection.execute(
                        f"SELECT {id_column} FROM {source} WHERE {id_column}=?", int(saved)).fetchone()
                if not anchor:
                    raise RuntimeError("MirrorSourceHistoryChanged:" + stream)
            if stream == "events":
                stamp, source_id = saved or ["1900-01-01T00:00:00", 0]
                sql = f"""SELECT TOP ({batch_size}) {','.join(fields)} FROM {source}
                    WHERE FirstSeen>=DATEADD(day,-?,SYSDATETIME())
                    AND (UpdatedAt>? OR (UpdatedAt=? AND EventId>?))
                    ORDER BY UpdatedAt,EventId"""
                rows = source_connection.execute(sql, mirror.retention_days, stamp, stamp, source_id).fetchall()
                position = [json_value(rows[-1][3]), int(rows[-1][0])] if rows else [stamp, source_id]
            else:
                source_id = int(saved or 0)
                rows = source_connection.execute(f"""SELECT TOP ({batch_size}) {','.join(fields)} FROM {source}
                    WHERE {id_column}>? AND [{time_column}]>=DATEADD(day,-?,SYSDATETIME()) ORDER BY {id_column}""",
                    source_id, mirror.retention_days).fetchall()
                position = int(rows[-1][0]) if rows else source_id
            prepared = [(int(row[0]), row[1], {key: json_value(value) for key, value in zip(fields, row)}) for row in rows]
            mirror.commit_batch(stream, prepared, position, caught_up=len(rows) < batch_size)
            result[stream] = len(prepared)
        mirror.maintenance()
        return result
    finally:
        if task_connection is not None and task_connection is not connection:
            task_connection.close()
        connection.close()
