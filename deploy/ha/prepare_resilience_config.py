"""Prepare a new config without overwriting the running config or moving spools."""
from __future__ import annotations
import argparse
import copy
import json
import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from common.replicated_ingest import validate_peers


def prepare(cfg,environment=None):
    result=copy.deepcopy(cfg);environment=environment or {}
    validate_peers(result["nodes"],result["node_id"])
    env=result.setdefault("env",{})
    for key in ("RFID_SPOOL_PATH","RFID_VIDEO_SPOOL"):
        value=env.get(key) or environment.get(key)
        if not value:
            raise ValueError("ExistingAbsoluteSpoolPathsRequired")
        # pathlib on the actual target OS determines its absolute path semantics.
        if not Path(value).is_absolute():raise ValueError("ExistingAbsoluteSpoolPathsRequired")
        env[key]=value
    result.update(replication_enabled=True,fallback_enabled=True,fallback_retention_days=93,
                  probation_sec=180,probation_stall_sec=120)
    result.setdefault("hardware_fencing_required",False)
    return result


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument("--input",required=True,type=Path);p.add_argument("--output",required=True,type=Path)
    a=p.parse_args(argv)
    if a.output.exists() or a.output.resolve()==a.input.resolve():raise ValueError("NewOutputFileRequired")
    cfg=prepare(json.loads(a.input.read_text(encoding="utf-8-sig")),os.environ)
    descriptor=os.open(str(a.output),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,"w",encoding="utf-8") as file:json.dump(cfg,file,indent=2)
    print("RESILIENCE_CONFIG_PREPARED",cfg["node_id"])


if __name__=="__main__":
    try:raise SystemExit(main())
    except Exception as exc:print("RESILIENCE_CONFIG_FAILED",type(exc).__name__,file=sys.stderr);raise SystemExit(2)
