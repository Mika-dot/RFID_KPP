"""Read-only installation verification. Does not inject events or switch owners."""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from guardian.net import json_request
from observer.mirror import REQUIRED_STREAMS


def verify_local_copies(node, status, token, request):
    mirror = status.get("metadata_mirror", {})
    if (status.get("fallback_enabled") is not True or mirror.get("enabled") is not True
            or mirror.get("error") or type(mirror.get("retention_days")) is not int
            or mirror["retention_days"] < 93):
        raise RuntimeError("LocalMetadataCopiesUnavailable")
    for stream in REQUIRED_STREAMS:
        info = mirror.get("streams", {}).get(stream, {})
        if (info.get("caught_up") is not True or type(info.get("age_sec")) not in (int, float)
                or not 0 <= info["age_sec"] <= 130 or type(info.get("records")) is not int):
            raise RuntimeError("LocalMetadataCopiesNotCurrent:" + stream)
    code, archive = request(node["url"].rstrip("/") + "/fallback/stats", token, timeout=3)
    if (code != 200 or type(archive.get("retention_days")) is not int or archive["retention_days"] < 93
            or type(archive.get("pending")) is not int or archive["pending"] < 0):
        raise RuntimeError("LocalFallbackJournalUnavailable")
    code, recent = request(node["url"].rstrip("/") + "/events/recent", token, timeout=3)
    if (code != 200 or recent.get("node") != node["id"] or recent.get("configured") is not True
            or recent.get("stale") is not False or recent.get("caught_up") is not True):
        raise RuntimeError("LocalFallbackViewNotCurrent")
    return {"verified": True, "retention_days": mirror["retention_days"],
            "streams": list(REQUIRED_STREAMS), "pending": archive["pending"],
            "historical_coverage": "bounded_backfill_not_a_complete_sql_backup"}


def verify(nodes,release,token,request=json_request,require_local_copies=False):
    results={};active=[]
    for node in nodes:
        code,status=request(node["url"].rstrip("/")+"/status",token,timeout=3)
        if (code!=200 or status.get("node")!=node["id"] or status.get("release_sha")!=release
            or status.get("fencing_protocol")!=2 or status.get("replication_enabled") is not True
            or not 0<=status.get("sample_age",999)<=10 or status.get("faulted") is not False
            or status.get("operator_maintenance") is not False or status.get("prepared") is not True):
            raise RuntimeError("NodeInstallationNotReady")
        if status.get("active"):
            if status.get("healthy") is not True or len(status.get("services",{}))!=5 or not all(s.get("ok") for s in status["services"].values()):
                raise RuntimeError("ExecutorServicesNotReady")
            active.append((node["id"],status["epoch"]))
        code,replica=request(node["url"].rstrip("/")+"/replica/stats",token,timeout=3)
        if code!=200 or type(replica.get("pending")) is not int:raise RuntimeError("ReplicaJournalUnavailable")
        results[node["id"]]={"release":release,"prepared":True,"epoch":status["epoch"],"active":bool(status.get("active")),"replica_pending":replica["pending"],"update_pending":bool(status.get("update",{}).get("pending",False))}
        if require_local_copies:
            results[node["id"]]["local_copies"] = verify_local_copies(node, status, token, request)
    if len(active)!=1 or any(v["epoch"]!=active[0][1] for v in results.values()):
        raise RuntimeError("ClusterOwnershipInconsistent")
    return {"installation_ready":True,"nodes":results,"owner":active[0][0],"epoch":active[0][1],
            "local_copy_verification": "passed" if require_local_copies else "not_requested",
            "physical_business_acceptance":"not_measured"}


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument("--nodes-config",type=Path,required=True);p.add_argument("--release",required=True)
    p.add_argument("--gateway-url");p.add_argument("--observer-url");p.add_argument("--output",type=Path)
    p.add_argument("--require-local-copies", action="store_true", help="Require fresh caught-up six-stream mirrors and >=93-day journals on all nodes")
    a=p.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}",a.release):raise ValueError("ExactReleaseRequired")
    token=os.environ["PERIMETER_HA_TOKEN"]
    proof=verify(json.loads(a.nodes_config.read_text(encoding="utf-8-sig"))["nodes"],a.release,token,
                 require_local_copies=a.require_local_copies)
    if a.gateway_url:
        code,_=json_request(a.gateway_url.rstrip("/")+"/health/ready",timeout=3)
        if code!=200:raise RuntimeError("GatewayNotReady")
        proof["gateway_ready"]=True
    if a.observer_url:
        code,snapshot=json_request(a.observer_url.rstrip("/")+"/status",token,timeout=3)
        if code!=200 or snapshot.get("stale") or snapshot.get("unavailable_sources") or snapshot.get("status") in {"starting","collector_error","critical"}:
            raise RuntimeError("BehaviorObserverNotReady")
        proof["observer_ready"]=True;proof["baseline_status"]=snapshot["status"]
    if a.output:
        a.output.write_text(json.dumps(proof,indent=2)+"\n",encoding="utf-8")
    print("INSTALLATION_FEATURES_READY",json.dumps(proof,sort_keys=True))
    return 0


if __name__=="__main__":
    try:raise SystemExit(main())
    except Exception as exc:print("INSTALLATION_VERIFICATION_FAILED",type(exc).__name__,file=sys.stderr);raise SystemExit(2)
