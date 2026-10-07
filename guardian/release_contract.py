"""Protected, offline business checks for future automatic releases.

The currently installed checker examines candidate source in a separate process.
Candidate tests cannot remove these checks. No reader, SQL or production writes.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

MANIFEST = "guardian/runtime_contract.json"
BASE_FLOOR = {"version": 1, "business_generation": 1,
              "capabilities": ["rfid-causal-activity-evidence", "rfid-historical-latch-evidence"]}


def parse_contract(value):
    if (not isinstance(value, dict) or set(value) != set(BASE_FLOOR)
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["business_generation"]) is not int or value["business_generation"] < 1
            or not isinstance(value["capabilities"], list)
            or any(not isinstance(x, str) or not x for x in value["capabilities"])
            or len(set(value["capabilities"])) != len(value["capabilities"])):
        raise RuntimeError("CandidateRuntimeContractInvalid")
    return value


def raise_floor(existing=None, accepted=None):
    values = [BASE_FLOOR]
    if existing is not None:
        values.append(parse_contract(existing))
    if accepted is not None:
        values.append(parse_contract(accepted))
    return {"version": 1, "business_generation": max(x["business_generation"] for x in values),
            "capabilities": sorted(set().union(*(x["capabilities"] for x in values)))}


def validate_contract(text, floor):
    try:
        value = parse_contract(json.loads(text))
    except (ValueError, TypeError):
        raise RuntimeError("CandidateRuntimeContractInvalid") from None
    floor = raise_floor(floor)
    if (value["business_generation"] < floor["business_generation"]
            or not set(floor["capabilities"]).issubset(value["capabilities"])):
        raise RuntimeError("CandidateBusinessDowngrade")
    return value


def docs_only(paths):
    # Conservative: requirements, models, monitoring scripts and configuration
    # are runtime changes even when they live in a documentation directory.
    return all(Path(path).suffix.lower() in {".md", ".rst"} for path in paths)


def verify_business(root):
    name = "perimeter_candidate_business_flow"
    spec = importlib.util.spec_from_file_location(name, Path(root) / "common/business_flow.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    at = datetime(2026, 10, 6, 18, 2, 39)
    assess = module.assess_rfid_flow
    args = dict(now=at+timedelta(seconds=1100), rfid_at=at,
                video_at=at-timedelta(seconds=2), video_recent_events=1,
                warehouse_recent_events=33, warehouse_at=at-timedelta(seconds=170),
                stall_seconds=900)
    quiet = assess(**args)
    if quiet.latch_fault or quiet.status == "unavailable":
        raise RuntimeError("CandidateOldActivityCausesFalseFault")
    equal = assess(**dict(args, video_at=at, warehouse_at=at))
    if equal.latch_fault or equal.status == "unavailable":
        raise RuntimeError("CandidateSamePassageCausesFalseFault")
    later = assess(**dict(args, video_at=at+timedelta(seconds=900),
                         warehouse_at=at+timedelta(seconds=950), video_recent_events=3))
    if later.status != "unavailable" or not later.latch_fault:
        raise RuntimeError("CandidateRealMissingReadsIgnored")
    latched = dict(args, fault_latched=True,
                   latched_rfid_marker=module.source_marker(at),
                   last_fault_detail="rfid_stale_while_video_active",
                   video_recent_events=0, video_history_complete=True)
    contradicted = assess(**latched)
    if (contradicted.status != "degraded" or contradicted.clear_latch
            or contradicted.detail != "rfid_historical_activity_evidence_contradicted"):
        raise RuntimeError("CandidateHistoricalLatchRegression")
    real_latch = assess(**dict(latched, video_at=at+timedelta(seconds=1)))
    if real_latch.status != "unavailable" or real_latch.clear_latch:
        raise RuntimeError("CandidateRealLatchDiscarded")
    unknown = assess(**dict(latched, video_history_complete=False))
    if unknown.status != "unavailable" or unknown.clear_latch:
        raise RuntimeError("CandidateMissingEvidenceDiscardsLatch")
    return 6


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)
    try:
        checks = verify_business(args.candidate)
    except Exception:
        # Candidate exceptions may contain configuration values.
        print("CANDIDATE_PROTECTED_BUSINESS_CHECK_FAILED")
        return 2
    print("CANDIDATE_PROTECTED_BUSINESS_CHECKS_OK", checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
