"""Read-only operator acceptance for pinned HA rollout and real business traffic.

Use --snapshot for an immediate report, --baseline NEW.json before an actual
RFID passage, and --check BASELINE.json afterwards. No synthetic data, leases,
service restarts, SDK calls, or state modifications. Evidence has no credentials,
tag IDs, persons, photographs, or raw event rows.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.ha.diagnose_cluster import diagnose
from deploy.ha.release_update import load_environment
from guardian.net import json_request

NODES = {"physical", "perimetr", "comparator"}
SERVICES = {"RfidReader", "RusGuardSync", "Yolo", "Aggregator", "WebDashboard"}
KIND = "perimeter-pinned-factory-acceptance-v1"


def check_cluster(report, release):
    nodes = report["nodes"]
    reasons = []
    if set(nodes) != NODES or not report["all_nodes_fresh"]:
        reasons.append("nodes_not_fresh")
    if report["mixed_releases"] or any(n.get("release") != release for n in nodes.values()):
        reasons.append("release_mismatch")
    if not report["all_nodes_pinned"]:
        reasons.append("operator_pin_not_verified")
    if any(n.get("qualification_passed") is not True for n in nodes.values()):
        reasons.append("qualification_not_verified")
    if not report["epoch_consistent"]:
        reasons.append("epoch_inconsistent")
    if any(not n.get("replication_enabled") or not n.get("fallback_enabled") for n in nodes.values()):
        reasons.append("replication_or_fallback_missing")
    if any(n.get("operator_maintenance") is not False for n in nodes.values()):
        reasons.append("maintenance_active")
    owners = report["reported_active"]
    if len(owners) != 1 or report["healthy_owner"] != owners[0]:
        reasons.append("unique_healthy_owner_missing")
    elif (not all(nodes[owners[0]]["services"][name]["ready"] is True for name in SERVICES)
          or nodes[owners[0]]["faulted"] is not False):
        reasons.append("owner_workers_not_ready")
    # Faulted passive receivers are intentionally not treated as a failed
    # code deployment; their standby eligibility is a separate reliability KPI.
    return {"passed": not reasons, "reasons": reasons,
            "owner": owners[0] if len(owners) == 1 else None,
            "epoch": nodes[owners[0]].get("epoch") if len(owners) == 1 else None,
            "faulted_passive": [n for n in NODES if nodes.get(n, {}).get("faulted")
                               and n not in owners]}


def sql_snapshot(connection_string):
    import pyodbc
    pyodbc.pooling = False
    conn = pyodbc.connect(connection_string, timeout=5, autocommit=True)
    try:
        conn.timeout = 5
        lease = conn.execute(
            "SELECT Owner,Epoch,CASE WHEN Enabled=1 AND ExpiresAt>SYSUTCDATETIME() "
            "THEN 1 ELSE 0 END FROM dbo.KPP_HA_Lease WHERE Id=1").fetchone()
        controller = conn.execute(
            "SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
            "FROM dbo.KPP_HA_Controller WHERE Id=1").fetchone()
        source_id = conn.execute(
            "SELECT ISNULL(MAX(Id),0) FROM dbo.RFID_Tags").fetchone()[0]
        cursor_row = conn.execute(
            "SELECT StateValue FROM dbo.KPP_RuntimeState "
            "WHERE StateKey='LAST_RFID_ID_V3'").fetchone()
        event = conn.execute(
            "SELECT TOP(1) EventId,RfidMaxRawId,VideoMatched,WarehouseId,FinalDirection "
            "FROM dbo.KPP_ReelEvents WHERE IsReel=1 ORDER BY EventId DESC").fetchone()
        event_id = int(event[0]) if event else 0
        reel = ({"event_id":event_id,
                 "rfid_max_raw_id":int(event[1]) if event[1] is not None else None,
                 "video_matched":bool(event[2]),
                 "warehouse_linked":event[3] is not None,
                 "direction_present":event[4] not in (None,"","UNKNOWN")}
                if event else None)
        if lease is None or controller is None or cursor_row is None:
            raise ValueError("missing_required_sql_state")
        return {"owner": lease[0], "epoch": int(lease[1]), "lease_valid":bool(lease[2]),
                "controller":controller[0], "controller_valid":bool(controller[1]),
                "rfid_max_id":int(source_id), "aggregator_cursor":int(cursor_row[0]),
                "event_max_id":event_id, "recent_reel":reel}
    finally:
        conn.close()


def web_snapshot(node_url, env, reel_id=None):
    parsed = urllib.parse.urlparse(node_url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("invalid_configured_node_url")
    port = int(env.get("KPP_WEB_PORT", "5050"))
    if port < 1 or port > 65535:
        raise ValueError("invalid_web_port")
    base = "http://" + parsed.hostname + ":" + str(port)
    headers = {"Accept": "application/json"}
    username, password = env.get("KPP_WEB_AUTH_USER"), env.get("KPP_WEB_AUTH_PASSWORD")
    if username is not None and password is not None:
        auth = base64.b64encode((username + ":" + password).encode()).decode("ascii")
        headers["Authorization"] = "Basic " + auth

    def get(path):
        req = urllib.request.Request(base + path, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError("web_http_not_200")
            return json.loads(response.read(256000).decode("utf-8"))

    health = get("/api/health")
    events = get("/api/events?limit=1&offset=0")
    if not isinstance(health, dict) or not isinstance(events, dict):
        raise ValueError("invalid_web_response")
    total = events.get("total")
    if type(total) is not int or total < 0 or not isinstance(events.get("items"), list):
        raise ValueError("web_events_unavailable")
    detail = None
    if type(reel_id) is int and reel_id > 0:
        # Same concrete reel event visible through Web, not merely an increase
        # in unrelated global counts. Do not export sensitive event fields.
        detail = get("/api/event/" + str(reel_id))
    exact_id = (int(detail.get("EventId", -1)) if isinstance(detail,dict)
                and str(detail.get("EventId","")).isdigit() else None)
    return {"health_http_ok": True, "events_http_ok": True, "events_total": total,
            "exact_event_id":exact_id,
            "event_rfid_raw_id":(int(detail["RfidMaxRawId"]) if isinstance(detail,dict)
                and detail.get("RfidMaxRawId") is not None else None),
            "health_status": health.get("status") if health.get("status") in {"OK","WARN","WARNING","DEGRADED","ERROR"} else "unknown"}


def compare_snapshots(baseline, current):
    old, new = baseline["sql"], current["sql"]
    source_growth = new["rfid_max_id"] - old["rfid_max_id"]
    event_growth = new["event_max_id"] - old["event_max_id"]
    old_web, new_web = baseline.get("web") or {}, current.get("web") or {}
    web_growth = (new_web.get("events_total") - old_web.get("events_total")
                  if type(new_web.get("events_total")) is int
                  and type(old_web.get("events_total")) is int else None)
    # A busy checkpoint need not drain its entire queue. Crossing the baseline
    # raw ID proves that at least one post-baseline read was processed.
    source_to_aggregator = (source_growth > 0 and
                            new["aggregator_cursor"] > old["rfid_max_id"])
    reel = new.get("recent_reel") or {}
    # A new RFID read must belong to this *same* reel event and be visible
    # under its exact EventId on Web. This is stronger than two totals rising.
    correlated = (event_growth > 0 and type(reel.get("rfid_max_raw_id")) is int
                  and reel["rfid_max_raw_id"] > old["rfid_max_id"]
                  and new["aggregator_cursor"] >= reel["rfid_max_raw_id"]
                  and new_web.get("exact_event_id") == new["event_max_id"]
                  and new_web.get("event_rfid_raw_id") == reel["rfid_max_raw_id"])
    event_to_web = bool(correlated and web_growth is not None and web_growth > 0)
    return {"rfid_new_rows": max(0, source_growth),
            "aggregator_progress_after_baseline": bool(source_to_aggregator),
            "aggregator_cursor_caught_up":new["aggregator_cursor"] >= new["rfid_max_id"],
            "aggregator_backlog_estimate":max(0,new["rfid_max_id"]-new["aggregator_cursor"]),
            "new_business_events": max(0, event_growth),
            "web_event_count_growth": web_growth,
            "rfid_sql_aggregator_observed": bool(source_to_aggregator),
            "event_web_progress_observed": bool(event_to_web),
            "last_reel_video_matched": reel.get("video_matched"),
            "last_reel_warehouse_linked": reel.get("warehouse_linked"),
            "last_reel_direction_present": reel.get("direction_present"),
            "full_identity_direction_video_warehouse_proof": "not_measured",
            "no_events_is_not_install_failure": True}


def collect(cfg, env, release, request=json_request, sql_reader=sql_snapshot, web_reader=web_snapshot):
    cluster = diagnose(cfg["nodes"], env["PERIMETER_HA_TOKEN"], request)
    gate = check_cluster(cluster, release)
    mirrors = {name: row.get("metadata_mirror", {})
               for name, row in cluster["nodes"].items()}
    mirror_warnings = [name for name, mirror in mirrors.items()
                       if mirror.get("enabled") is not True
                       or mirror.get("reported_error") is True
                       or mirror.get("blocked_stream_count") not in (0,)]
    outcome = {"read_only":True, "kind":KIND, "release":release,
               "observed_at_utc":datetime.now(timezone.utc).isoformat(),
               "cluster":cluster, "deployment":gate,
               "redundancy":{"replication_configured":all(
                    row.get("replication_enabled") and row.get("fallback_enabled")
                    for row in cluster["nodes"].values()),
                    "metadata_mirror_warnings":mirror_warnings,
                    "faulted_passive":gate["faulted_passive"],
                    "actual_failover_test":"not_performed"},
               "sql":None, "web":None, "checks":[]}
    try:
        outcome["sql"] = sql_reader(env["PERIMETER_HA_SQL"])
    except Exception as exc:
        outcome["checks"].append("sql_" + type(exc).__name__)
    if outcome["sql"] is not None:
        sql = outcome["sql"]
        if (sql["owner"] != gate["owner"] or sql["epoch"] != gate["epoch"]
                or not sql["lease_valid"] or sql["controller"] != "comparator"
                or not sql["controller_valid"]):
            outcome["checks"].append("sql_lease_or_controller_inconsistent")
    if gate["owner"] is not None:
        node = next(n for n in cfg["nodes"] if n["id"] == gate["owner"])
        try:
            reel_id = outcome["sql"]["event_max_id"] if outcome["sql"] else None
            outcome["web"] = web_reader(node["url"],env,reel_id)
        except Exception as exc:
            outcome["checks"].append("web_" + type(exc).__name__)
    outcome["endpoints_ready"] = bool(gate["passed"] and outcome["sql"] and outcome["web"]
                                      and not outcome["checks"]
                                      and outcome["web"]["health_http_ok"]
                                      and outcome["web"]["events_http_ok"]
                                      and outcome["web"]["health_status"] != "ERROR")
    return outcome


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--nodes-config", type=Path, required=True)
    p.add_argument("--environment", type=Path, required=True)
    p.add_argument("--release", required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--snapshot", action="store_true")
    mode.add_argument("--baseline", type=Path)
    mode.add_argument("--check", type=Path)
    a = p.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}",a.release):
        raise ValueError("exact_release_required")
    cfg = json.loads(a.nodes_config.read_text(encoding="utf-8-sig"))
    env = load_environment(a, cfg)
    result = collect(cfg, env, a.release)
    if a.baseline:
        if not result["endpoints_ready"]:
            raise RuntimeError("baseline_requires_healthy_readonly_snapshot")
        evidence = {"kind":KIND, "release":a.release,
                    "created_at_utc":result["observed_at_utc"],
                    "sql":result["sql"], "web":result["web"]}
        with a.baseline.open("x",encoding="utf-8") as handle:
            json.dump(evidence, handle, indent=2)
        result["baseline_created"] = str(a.baseline)
    if a.check:
        baseline = json.loads(a.check.read_text(encoding="utf-8-sig"))
        if (baseline.get("kind") != KIND or baseline.get("release") != a.release
                or not isinstance(baseline.get("sql"),dict)):
            raise ValueError("baseline_does_not_match")
        if result["sql"]:
            result["business_delta"] = compare_snapshots(baseline, result)
    if a.check and result.get("business_delta"):
        delta = result["business_delta"]
        result["business_observed"] = bool(
            delta["rfid_sql_aggregator_observed"] and delta["event_web_progress_observed"])
        result["evidence_status"] = ("progress_observed_not_identity_verified" if
                                     result["business_observed"] else "business_progress_not_observed")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if not result["endpoints_ready"]:
        return 2
    if a.check and not result.get("business_observed"):
        return 3  # Unproven traffic, not a release rollback or forced fault.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FACTORY_ACCEPTANCE_UNAVAILABLE " + type(exc).__name__,file=sys.stderr)
        raise SystemExit(2)
