"""Aggregate counts only: no EPC, people, images or business writes."""
from __future__ import annotations
import os
import re
from concurrent.futures import ThreadPoolExecutor
from guardian.net import json_request


def table(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("InvalidObserverTable")
    return value


def sql_metrics(connection=None):
    if connection is None:
        import pyodbc
        connection = pyodbc.connect(os.environ["PERIMETER_OBSERVER_SQL"], timeout=3, autocommit=True, readonly=True)
    try:
        connection.timeout = 3
        connection.execute("SET LOCK_TIMEOUT 2000")
        start, end = connection.execute("SELECT DATEADD(minute,-5,SYSDATETIME()),SYSDATETIME()").fetchone()
        output = {}
        for name, env, default, column in (
            ("rfid_reads_5min", "OBSERVER_RFID_TABLE", "dbo.RFID_Tags", "RecordTime"),
            ("video_events_5min", "OBSERVER_VIDEO_TABLE", "dbo.ReelTransitions", "CapturedAt"),
            ("skud_events_5min", "OBSERVER_SKUD_TABLE", "dbo.RusGuardLogs", "CreatedAt"),
            ("warehouse_rows_5min", "OBSERVER_WAREHOUSE_TABLE", "dbo.Warehouse", "Dt"),
            ("task_rows_5min", "OBSERVER_TASK_TABLE", "dbo.RfidTags", "Dt")):
            source = table(os.getenv(env, default))
            output[name] = int(connection.execute(f"SELECT COUNT_BIG(*) FROM {source} WHERE {column}>=? AND {column}<?", start, end).fetchone()[0])
        events = table(os.getenv("OBSERVER_EVENTS_TABLE", "dbo.KPP_ReelEvents"))
        row = connection.execute(f"""SELECT COUNT_BIG(*),COUNT(DISTINCT CASE WHEN RfidReadCount>0 THEN PassageGroupKey END),
            SUM(CASE WHEN IsReel=1 AND RfidReadCount>0 THEN 1 ELSE 0 END),SUM(CONVERT(bigint,NeedRecheck)),
            SUM(CASE WHEN FinalDirection='UNKNOWN' THEN 1 ELSE 0 END),
            SUM(CASE WHEN IsReel=1 AND RfidReadCount>0 AND VideoMatched=1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN IsReel=1 AND RfidReadCount>0 AND SkudMatched=1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN SessionCloseReason='WAREHOUSE_ONLY' THEN 1 ELSE 0 END)
            FROM {events} WHERE LastSeen>=? AND LastSeen<?""", start, end).fetchone()
        for key, value in zip(("final_events_5min", "rfid_groups_5min", "rfid_reels_5min", "need_recheck_5min", "unknown_direction_5min", "video_matched_5min", "skud_matched_5min", "warehouse_only_5min"), row):
            output[key] = int(value or 0)
        raw = table(os.getenv("OBSERVER_RFID_TABLE", "dbo.RFID_Tags"))
        row = connection.execute(f"""SELECT COUNT(DISTINCT EPC),COUNT(DISTINCT CONCAT(EPC,TID)),AVG(TRY_CONVERT(float,RSSI)),
            SUM(CASE WHEN Antenna=1 THEN 1 ELSE 0 END),SUM(CASE WHEN Antenna=2 THEN 1 ELSE 0 END),
            SUM(CASE WHEN Antenna=3 THEN 1 ELSE 0 END),SUM(CASE WHEN Antenna=4 THEN 1 ELSE 0 END)
            FROM {raw} WHERE RecordTime>=? AND RecordTime<?""",start,end).fetchone()
        for key,value in zip(("rfid_unique_epc_5min","rfid_unique_epc_tid_5min","rfid_avg_rssi","rfid_antenna1_reads","rfid_antenna2_reads","rfid_antenna3_reads","rfid_antenna4_reads"),row):
            if value is not None:output[key]=float(value)
        wh=table(os.getenv("OBSERVER_WAREHOUSE_TABLE","dbo.Warehouse"))
        tasks=table(os.getenv("OBSERVER_TASK_TABLE","dbo.RfidTags"))
        row=connection.execute(f"""SELECT
            SUM(CASE WHEN NULLIF(LTRIM(RTRIM(w.Tag)),'') IS NOT NULL AND UPPER(w.Tag)=UPPER(e.SourceTag) THEN 1 ELSE 0 END),
            SUM(CASE WHEN NULLIF(LTRIM(RTRIM(w.Tag)),'') IS NULL AND w.Ids=t.Ids THEN 1 ELSE 0 END),
            SUM(CASE WHEN NULLIF(LTRIM(RTRIM(w.Tag)),'') IS NULL AND ISNULL(w.Ids,'')<>ISNULL(t.Ids,'') AND w.SeriesNumber=t.SeriesNumber THEN 1 ELSE 0 END)
            FROM {events} e JOIN {wh} w ON w.Id=e.WarehouseId LEFT JOIN {tasks} t ON t.Id=e.Task1CId
            WHERE e.LastSeen>=? AND e.LastSeen<?""",start,end).fetchone()
        for key,value in zip(("warehouse_tag_links_5min","warehouse_ids_links_5min","warehouse_series_links_5min"),row):
            output[key]=int(value or 0)
        maximum = connection.execute(f"SELECT ISNULL(MAX(Id),0) FROM {raw}").fetchone()[0]
        cursor = connection.execute("SELECT TRY_CONVERT(bigint,StateValue) FROM dbo.KPP_RuntimeState WHERE StateKey='LAST_RFID_ID_V3'").fetchone()
        if cursor and cursor[0] is not None:
            output["cursor_lag"] = max(0, int(maximum)-int(cursor[0]))
        return output
    finally:
        connection.close()


def cluster_metrics(nodes, token, request=json_request):
    def fetch(node):
        try:
            code, status = request(node["url"].rstrip("/")+"/status", token, timeout=2)
            if (code != 200 or status.get("node") != node["id"] or type(status.get("sample_age")) not in (int, float)
                    or not 0 <= status["sample_age"] <= 10):
                return node["id"], None
            return node["id"], status
        except Exception:
            return node["id"], None
    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = dict(pool.map(fetch, nodes))
    active = [(nid, s) for nid, s in statuses.items() if s and s.get("active")]
    metrics = {"ha_reachable_nodes": sum(s is not None for s in statuses.values()),
               "ha_active_nodes": len(active), "ha_ready_reserves": sum(bool(s and not s.get("active") and s.get("prepared") and not s.get("faulted")) for s in statuses.values())}
    metrics["ha_healthy_executors"] = sum(bool(s.get("healthy")) for _,s in active)
    for node in nodes:
        s = statuses[node["id"]]
        if not s:
            continue
        metrics["ha_faulted_"+node["id"]] = int(s.get("faulted", False))
        update = s.get("update", {})
        metrics["ha_quarantined_"+node["id"]] = update.get("quarantined", 0)
        metrics["ha_update_pending_"+node["id"]] = int(update.get("pending", False))
        for kind,value in s.get("event_counts",{}).items():
            if kind in {"failover","failback","rolling_update","repair_step","repair_verified","repair_failed","update_candidate_rejected","update_confirmed"}:
                metrics["ha_"+kind+"_"+node["id"]]=value
        timing=s.get("recovery_timing")
        if timing and timing.get("readiness_rto_sec") is not None:
            metrics["ha_last_readiness_rto_sec_"+node["id"]]=timing["readiness_rto_sec"]
        if s.get("replication_enabled"):
            try:
                code, replica = request(node["url"].rstrip("/")+"/replica/stats", token, timeout=2)
                if code == 200:
                    metrics["replica_pending_"+node["id"]] = replica["pending"]
            except Exception:
                pass
    if len(active) == 1:
        owner, status = active[0]
        bus = status.get("bus_metrics", {}) if 0 <= status.get("bus_sample_age",999) <= 15 else {}
        for key, value in bus.items():
            metrics[key] = value
        if "rfid_pending" in bus and "video_pending" in bus:
            metrics["spool_pending"] = bus["rfid_pending"]+bus["video_pending"]
        pending = 0
        observed = False
        for service in status.get("services", {}).values():
            m = service.get("detail", {}).get("metrics", {})
            if "spool_pending" in m:
                pending += m["spool_pending"]
                observed = True
        if observed:
            metrics["spool_pending"] = pending
    return metrics, statuses
