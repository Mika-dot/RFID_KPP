"""GET-only, bounded cluster diagnosis without logs, credentials or event data."""
from __future__ import annotations
import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0, str(ROOT))
from guardian.config import SERVICES
from guardian.net import json_request

METRICS = ("business_flow_latched", "rfid_source_age_seconds", "self_heal_restarts",
           "self_heal_continues", "recovery_request_pending", "recovery_last_attempt")


def diagnose(nodes, token, request=json_request):
    rows = {}
    for node in nodes:
        try:
            code, status = request(node["url"].rstrip("/")+"/status", token, timeout=3)
            if code != 200 or status.get("node") != node["id"]:
                raise ValueError("InvalidNodeResponse")
            age = status.get("sample_age")
            fresh = (type(age) in (int, float) and 0 <= age <= 10
                     and type(status.get("fencing_protocol")) is int and status["fencing_protocol"] == 2)
            release = status.get("release_sha")
            row = {"fresh": fresh, "release": release if isinstance(release, str) and re.fullmatch(r"[0-9a-f]{40}", release) else None,
                   "epoch": status.get("epoch") if type(status.get("epoch")) is int else None}
            for key in ("active", "healthy", "prepared", "faulted", "operator_maintenance"):
                row[key] = status.get(key) if type(status.get(key)) is bool else None
            row["failed_preflight"] = [key for key, value in status.get("preflight", {}).get("checks", {}).items()
                                       if value is not True]
            row["verification_required"] = status.get("repair", {}).get("verification_required") is True
            row["update_pending"] = status.get("update", {}).get("pending") is True
            row["services"] = {}
            for name in SERVICES:
                service = status.get("services", {}).get(name, {})
                detail = service.get("detail", {})
                row["services"][name] = {"observed": "ok" in service, "ready": service.get("ok") is True,
                    "failed_dependencies": [key for key, value in detail.get("dependencies", {}).items()
                        if isinstance(value, dict) and value.get("status") not in {"ok", "disabled"}],
                    "metrics": {key: value for key, value in detail.get("metrics", {}).items()
                        if key in METRICS and (type(value) is bool or type(value) in (int, float) and math.isfinite(value) and value >= 0)}}
            rows[node["id"]] = row
        except Exception as exc:
            rows[node["id"]] = {"fresh":False, "error":type(exc).__name__}
    owners = [key for key, row in rows.items() if row.get("fresh") and row.get("active")]
    healthy = [key for key in owners if rows[key].get("healthy") and not rows[key].get("faulted")]
    versions = {row.get("release") for row in rows.values() if row.get("fresh")}
    return {"read_only":True, "nodes":rows, "reported_active":owners,
        "healthy_owner":healthy[0] if len(owners) == len(healthy) == 1 else None,
        "mixed_releases":len(versions)>1, "live_restoration_confirmed":False,
        "physical_business_acceptance":"not_measured"}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--nodes-config", type=Path, required=True)
    credentials = p.add_mutually_exclusive_group()
    credentials.add_argument("--token-file", type=Path)
    credentials.add_argument("--environment", type=Path, help="Existing private Linux environment or Windows transfer bundle")
    p.add_argument("--output", type=Path)
    args = p.parse_args(argv)
    cfg = json.loads(args.nodes_config.read_text(encoding="utf-8-sig"))
    if args.environment:
        from deploy.ha.release_update import load_environment
        token = load_environment(args, cfg)["PERIMETER_HA_TOKEN"]
    elif args.token_file:
        path = args.token_file
        if (path.is_symlink() or not path.is_file() or (os.name != "nt" and
                (path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid()))):
            raise RuntimeError("PrivateTokenFileRequired")
        token = path.read_text(encoding="utf-8-sig").strip()
    else:
        token = os.environ["PERIMETER_HA_TOKEN"]
    if len(token) < 32 or token.startswith("CHANGE_"):
        raise ValueError("ExistingClusterCredentialsRequired")
    proof = diagnose(cfg["nodes"], token)
    raw = json.dumps(proof, ensure_ascii=False, indent=2, allow_nan=False)+"\n"
    if args.output:args.output.write_text(raw, encoding="utf-8")
    print(raw, end="")
    return 0


if __name__ == "__main__":
    try:raise SystemExit(main())
    except Exception as exc:
        print("CLUSTER_DIAGNOSIS_FAILED", type(exc).__name__, file=sys.stderr)
        raise SystemExit(2)
