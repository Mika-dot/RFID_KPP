"""Aggregate counts only: no EPC, people, images or business writes."""
from __future__ import annotations
import os
import re
from concurrent.futures import ThreadPoolExecutor
from guardian.net import json_request
from observer.catalog import RUNTIME_FIELDS


def table(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("InvalidObserverTable")
    return value


def sql_metrics(connection=None, task_connection=None, row_limit=20000):
    """SELECT-only, independent sources; a failed optional bus stays unknown."""
    from observer.detailed import summarize, number
    if type(row_limit) is not int or not 1 <= row_limit <= 50000:
        raise ValueError("InvalidObserverRowLimit")
    if connection is None:
        import pyodbc
        connection = pyodbc.connect(os.environ["PERIMETER_OBSERVER_SQL"], timeout=3, autocommit=True, readonly=True)
    output, failures = {}, []
    try:
        connection.timeout = 3
        connection.execute("SET LOCK_TIMEOUT 2000")
        destination = os.getenv("PERIMETER_OBSERVER_TASK_SQL") or os.getenv("KPP_TASK_CONN_STR")
        tasks_db = task_connection if task_connection is not None else connection
        try:
            if task_connection is None and destination:
                import pyodbc
                task_connection = pyodbc.connect(destination, timeout=3, autocommit=True, readonly=True)
                tasks_db = task_connection
            if tasks_db is not connection:
                tasks_db.timeout = 3
                tasks_db.execute("SET LOCK_TIMEOUT 2000")
        except Exception as exc:
            tasks_db = None  # Never silently query the wrong database.
            failures.append("task_connection:" + type(exc).__name__)
        start, end = connection.execute("SELECT DATEADD(minute,-5,SYSDATETIME()),SYSDATETIME()").fetchone()
        sources = {
            "rfid": (connection, table(os.getenv("OBSERVER_RFID_TABLE", "dbo.RFID_Tags")), "RecordTime", "Id,RecordTime,Antenna,RSSI,EPC,TID,TimeQuality,ReceivedAt"),
            "video": (connection, table(os.getenv("OBSERVER_VIDEO_TABLE", "dbo.ReelTransitions")), "CapturedAt", "Id,CapturedAt,ReceivedAt,ToCamera,TimeDiffSec,ReelCount"),
            "skud": (connection, table(os.getenv("OBSERVER_SKUD_TABLE", "dbo.RusGuardLogs")), "CreatedAt", "ExternalId2,CreatedAt,ReceivedAt,Direction,PersonControlDeviceName,CASE WHEN ExternalUserGuid IS NOT NULL AND PassExternalId2 IS NOT NULL THEN 1 ELSE 0 END AS HasIdentity"),
            "events": (connection, table(os.getenv("OBSERVER_EVENTS_TABLE", "dbo.KPP_ReelEvents")), "LastSeen", "EventId,FirstSeen,LastSeen,CompletedAt,UpdatedAt,RfidReadCount,IsReel,PassageGroupKey,NeedRecheck,FinalDirection,VideoMatched,SkudMatched,SessionCloseReason,WarningFlags,ConfidencePct,WarehouseId,WarehouseDt,VideoTimeDeltaMs/1000.0 AS VideoTimeDiffSec,SkudTimeDeltaMs/1000.0 AS SkudTimeDiffSec,SourceTag,Task1CId"),
            "warehouse": (tasks_db, table(os.getenv("OBSERVER_WAREHOUSE_TABLE", "dbo.Warehouse")), "Dt", "Id,Dt,Tag,Ids,SeriesNumber"),
            "tasks": (tasks_db, table(os.getenv("OBSERVER_TASK_TABLE", "dbo.RfidTags")), "Dt", "Id,Dt,Tag,Ids,SeriesNumber"),
        }
        populations = {}
        count_names = {"rfid":"rfid_reads_5min", "video":"video_events_5min", "skud":"skud_events_5min", "events":"final_events_5min", "warehouse":"warehouse_rows_5min", "tasks":"task_rows_5min"}
        for stream, (db, source, stamp, fields) in sources.items():
            try:
                output[count_names[stream]] = int(db.execute(f"SELECT COUNT_BIG(*) FROM {source} WHERE [{stamp}]>=? AND [{stamp}]<?", start, end).fetchone()[0])
                cursor = db.execute(f"SELECT TOP ({row_limit + 1}) {fields} FROM {source} WHERE [{stamp}]>=? AND [{stamp}]<? ORDER BY [{stamp}]", start, end)
                rows = cursor.fetchall()
                if len(rows) > row_limit:
                    failures.append(stream + ":population_truncated")
                    continue
                names = [column[0] for column in cursor.description]
                population = [dict(zip(names, row)) for row in rows]
                populations[stream] = population
                output.update(summarize(stream, population))
            except Exception as exc:
                failures.append(stream + ":" + type(exc).__name__)
        events = populations.get("events")
        if events is not None:
            physical = [r for r in events if r.get("IsReel") and (r.get("RfidReadCount") or 0) > 0]
            output.update(rfid_groups_5min=len({r["PassageGroupKey"] for r in events if (r.get("RfidReadCount") or 0)>0 and r.get("PassageGroupKey")}),
                rfid_reels_5min=len(physical), need_recheck_5min=sum(bool(r.get("NeedRecheck")) for r in events),
                unknown_direction_5min=sum(r.get("FinalDirection") == "UNKNOWN" for r in events),
                video_matched_5min=sum(bool(r.get("VideoMatched")) for r in physical),
                skud_matched_5min=sum(bool(r.get("SkudMatched")) for r in physical),
                warehouse_only_5min=sum(r.get("SessionCloseReason") == "WAREHOUSE_ONLY" for r in events))
        raw = populations.get("rfid")
        if raw is not None:
            output["rfid_unique_epc_5min"] = len({r["EPC"] for r in raw if r.get("EPC")})
            output["rfid_unique_epc_tid_5min"] = len({(r["EPC"], r.get("TID")) for r in raw if r.get("EPC")})
            rssi = [v for v in (number(r.get("RSSI")) for r in raw) if v is not None]
            if rssi:
                output["rfid_avg_rssi"] = sum(rssi) / len(rssi)
            for antenna in range(1,5):
                output[f"rfid_antenna{antenna}_reads"] = sum(r.get("Antenna") == antenna for r in raw)
        # Link lookup is by stored IDs across databases, never a local DB join.
        def linked_rows(stream, ids):
            db, source, _stamp, _fields = sources[stream]
            if db is None:
                raise RuntimeError("TaskConnectionUnavailable")
            ids = sorted({int(value) for value in ids if value is not None})
            result = {}
            for offset in range(0, len(ids), 500):
                batch = ids[offset:offset+500]
                rows = db.execute(f"SELECT Id,Tag,Ids,SeriesNumber FROM {source} WHERE Id IN ({','.join('?' for _ in batch)})", *batch).fetchall()
                result.update({int(r[0]):dict(zip(("Id","Tag","Ids","SeriesNumber"),r)) for r in rows})
            return result
        if events is not None:
            try:
                warehouse = linked_rows("warehouse", (r.get("WarehouseId") for r in events))
                tasks = linked_rows("tasks", (r.get("Task1CId") for r in events))
                links = {"tag":0,"ids":0,"series":0}
                norm = lambda value: str(value or "").strip().upper()
                for event in events:
                    w, t = warehouse.get(event.get("WarehouseId")), tasks.get(event.get("Task1CId"))
                    if not w:
                        continue
                    if norm(w["Tag"]) and norm(w["Tag"]) == norm(event.get("SourceTag")):
                        links["tag"] += 1
                    elif not norm(w["Tag"]) and t:
                        if norm(w["Ids"]) and norm(w["Ids"]) == norm(t["Ids"]):
                            links["ids"] += 1
                        elif norm(w["SeriesNumber"]) and norm(w["SeriesNumber"]) == norm(t["SeriesNumber"]):
                            links["series"] += 1
                output.update({"warehouse_"+kind+"_links_5min":count for kind,count in links.items()})
            except Exception as exc:
                failures.append("identity_links:"+type(exc).__name__)
        # Full history link existence checks are bounded by recent source IDs.
        if "warehouse" in populations:
            try:
                if tasks_db is None:
                    raise RuntimeError("TaskConnectionUnavailable")
                from datetime import timedelta
                from observer.detailed import ambiguous_series_count
                hours = float(os.getenv("KPP_TASK_MATCH_WINDOW_HOURS", "24"))
                if not 0 < hours <= 168:
                    raise ValueError("AmbiguityWindowInvalid")
                wanted = [r for r in populations["warehouse"] if not r.get("Tag") and r.get("SeriesNumber")]
                candidates = []
                series = sorted({r["SeriesNumber"] for r in wanted})
                if wanted:
                    lower = min(r["Dt"] for r in wanted) - timedelta(hours=hours)
                    upper = max(r["Dt"] for r in wanted) + timedelta(hours=hours)
                    task_table = sources["tasks"][1]
                    for offset in range(0,len(series),500):
                        batch = series[offset:offset+500]
                        rows = tasks_db.execute(f"SELECT TOP ({row_limit+1}) Tag,SeriesNumber,Dt FROM {task_table} WHERE SeriesNumber IN ({','.join('?' for _ in batch)}) AND Dt>=? AND Dt<=?",*batch,lower,upper).fetchall()
                        candidates.extend(rows)
                        if len(candidates) > row_limit:
                            raise RuntimeError("AmbiguityPopulationTruncated")
                output["warehouse_ambiguous_series_count"] = ambiguous_series_count(wanted,candidates,hours)
            except Exception as exc:
                failures.append("warehouse_ambiguity:"+type(exc).__name__)
        for stream, field, metric in (("video","VideoEventId","video_unmatched_ratio"), ("warehouse","WarehouseId","warehouse_unlinked_ratio")):
            if populations.get(stream):
                try:
                    ids = [r["Id"] for r in populations[stream]]
                    linked = set()
                    event_table = sources["events"][1]
                    for offset in range(0,len(ids),500):
                        batch = ids[offset:offset+500]
                        query = f"SELECT DISTINCT {field} FROM {event_table} WHERE {field} IN ({','.join('?' for _ in batch)})"
                        linked.update(int(r[0]) for r in connection.execute(query,*batch).fetchall())
                        if stream == "video":
                            link_table = table(os.getenv("KPP_VIDEO_LINK_TABLE", "dbo.KPP_EventVideoLinks"))
                            query = f"SELECT DISTINCT VideoEventId FROM {link_table} WHERE VideoEventId IN ({','.join('?' for _ in batch)})"
                            linked.update(int(r[0]) for r in connection.execute(query,*batch).fetchall())
                    output[metric] = sum(value not in linked for value in ids)/len(ids)
                except Exception as exc:
                    failures.append(metric+":"+type(exc).__name__)
        try:
            raw_table=sources["rfid"][1]
            maximum=connection.execute(f"SELECT ISNULL(MAX(Id),0) FROM {raw_table}").fetchone()[0]
            cursor=connection.execute("SELECT TRY_CONVERT(bigint,StateValue) FROM dbo.KPP_RuntimeState WHERE StateKey='LAST_RFID_ID_V3'").fetchone()
            if cursor and cursor[0] is not None:
                output["cursor_lag"]=max(0,int(maximum)-int(cursor[0]))
        except Exception as exc:
            failures.append("cursor:"+type(exc).__name__)
        output["_unavailable_sources"] = failures
        return output
    finally:
        if task_connection is not None and task_connection is not connection:
            task_connection.close()
        connection.close()


def cluster_metrics(nodes, token, request=json_request):
    def fetch(node):
        try:
            code, status = request(node["url"].rstrip("/")+"/status", token, timeout=2)
            if (code != 200 or status.get("node") != node["id"] or type(status.get("fencing_protocol")) is not int
                    or status["fencing_protocol"] != 2 or type(status.get("sample_age")) not in (int, float)
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
        for key in ("lease_renew_jitter", "controller_renew_jitter", "worker_restarts"):
            value = s.get("ha_runtime_metrics", {}).get(key)
            if type(value) in (int, float) and value >= 0:
                metrics[key + "_" + node["id"]] = value
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
        for service, (prefix, fields) in RUNTIME_FIELDS.items():
            detail = status.get("services", {}).get(service, {}).get("detail", {})
            for key in fields:
                value = detail.get("metrics", {}).get(key)
                if type(value) in (int, float) and value >= 0:
                    metrics[prefix + "_" + key] = value
        from observer.catalog import RUNTIME_METRICS
        for service in status.get("services", {}).values():
            detail = service.get("detail", {}).get("metrics", {})
            for key, value in detail.items():
                if any(key == stem or key.startswith(stem + "_") for stem in RUNTIME_METRICS):
                    if type(value) in (int, float):
                        import math
                        if math.isfinite(value) and value >= 0:
                            metrics[key] = value
    return metrics, statuses
