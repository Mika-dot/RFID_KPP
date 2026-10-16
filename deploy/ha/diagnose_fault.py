"""Read-only rollback/RFID diagnosis; never opens another reader connection."""
from __future__ import annotations
import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from deploy.ha.release_update import load_environment
from deploy.ha.diagnose_cluster import diagnose
from guardian.net import json_request
from guardian.repair import redact


def clean(text, env):
    for key,value in env.items():
        if ((any(word in key.upper() for word in ("PASSWORD","TOKEN","CONNECTION","CONN_STR","SECRET","RTSP"))
                or key.upper().endswith("_SQL"))
                and isinstance(value,str) and len(value)>=4):
            text=text.replace(value,"REDACTED")
    text=redact(text)
    text=re.sub(r"(?i)(epc|tid|source_?tag)\s*[=:]\s*[^\s;,]+",r"\1=REDACTED",text)
    text=re.sub(r"(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b","REDACTED_ID",text)
    return text


def tail(path, limit):
    try:
        if not path.is_file():return ""
        with path.open("rb") as file:
            file.seek(max(0,path.stat().st_size-limit))
            return file.read(limit).decode("utf-8","replace")
    except OSError:
        return ""  # A rotating log must not abort the rest of diagnosis.


def sanitize(value, env):
    if isinstance(value,str):return clean(value,env)
    if isinstance(value,dict):return {key:sanitize(item,env) for key,item in value.items()}
    if isinstance(value,list):return [sanitize(item,env) for item in value]
    if type(value) is float and not math.isfinite(value):return None
    return value


def collect(cfg,env,target,request=json_request,journal=None):
    state=Path(cfg["state_dir"])
    record=json.loads((state/"release.json").read_text(encoding="utf-8-sig"))
    result={"read_only":True,"node":cfg["node_id"],"release":{
        "current":record["current"]["sha"],"previous":(record.get("previous") or {}).get("sha"),
        "pending":record.get("pending"),"trial_started":record.get("trial_started"),
        "target_rejected":target in record.get("rejected",[]),
        "qualification":record.get("qualification",{}).get("status")},
        "reader":{"sdk_mode":env.get("RFID_SDK_MODE"),"ip":env.get("RFID_READER_IP"),
                  "port":env.get("RFID_READER_PORT","8888")},
        "cluster":diagnose(cfg["nodes"],env["PERIMETER_HA_TOKEN"],request)}
    own=next(n for n in cfg["nodes"] if n["id"]==cfg["node_id"])
    try:
        code,status=request(own["url"].rstrip("/")+"/status",env["PERIMETER_HA_TOKEN"],timeout=3)
        if code!=200 or status.get("node")!=cfg["node_id"]:raise ValueError("InvalidNodeResponse")
        result["rfid_dependencies"]={key:{field:value.get(field) for field in ("status","detail","age_seconds","stale_after_seconds")}
            for key,value in status.get("services",{}).get("RfidReader",{}).get("detail",{}).get("dependencies",{}).items()
            if key in {"rfid_reader","rfid_tcp","business_flow","database","local_spool","delivery_writer"} and isinstance(value,dict)}
        mirror = status.get("metadata_mirror", {})
        result["metadata_mirror"] = {key: mirror.get(key)
            for key in ("enabled", "error", "blocked_streams")}
    except Exception as exc:result["local_status_error"]=type(exc).__name__
    audit=[]
    for file in (state/"audit.previous.jsonl",state/"audit.jsonl"):
        for line in tail(file,128*1024).splitlines():
            try:item=json.loads(line)
            except ValueError:continue
            kind=item.get("kind","")
            if isinstance(kind,str) and (kind.startswith(("update_","candidate_","repair_"))
                    or kind in {"node_error","agent_resource_exhausted","promoted","demoted","mirror_sync_error","mirror_sync_blocked"}):
                audit.append({key:item[key] for key in ("time","kind","sha","reason","error","epoch","action","target","streams","native_code") if key in item})
    result["recent_guardian_events"]=audit[-30:]
    selected=[]
    pattern=re.compile(r"TCPConnect|connect_code|inventory|Wine|Exception|Error|Traceback|ReplicaQuorum|retry|controlled|reader connected|fatal|timeout|disconnected",re.I)
    for line in tail(state/"logs/RfidReader.log",24*1024).splitlines():
        if pattern.search(line):selected.append(line)
    result["rfid_log_signals"]=selected[-35:]
    if journal is not None:result["guardian_journal_signals"]=[line for line in journal.splitlines()
        if re.search(r"rollback|candidate_|update_rejected|Traceback|Error|Exception|resource_exhausted|node_error",line,re.I)][-30:]
    # Apply both known-secret replacement and field redaction to the complete
    # bounded output, including exceptions returned by legacy agents.
    return sanitize(result,env)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",type=Path,default=Path("/etc/perimeter/node.json"))
    p.add_argument("--environment",type=Path,default=Path("/etc/perimeter/environment"))
    p.add_argument("--release",required=True)
    a=p.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}",a.release):raise ValueError("ExactReleaseRequired")
    cfg=json.loads(a.config.read_text(encoding="utf-8-sig"));env=load_environment(a,cfg)
    journal=None
    if os.name!="nt":
        try:
            r=subprocess.run(["journalctl","-u","perimeter-guardian.service","--since=-20min","-n","120","--no-pager"],
                stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=10)
            if r.returncode==0:journal=r.stdout
        except (OSError,subprocess.TimeoutExpired):pass
    print(json.dumps(collect(cfg,env,a.release,journal=journal),ensure_ascii=False,indent=2,allow_nan=False))
    return 0


if __name__=="__main__":
    try:raise SystemExit(main())
    except Exception as exc:
        print("FAULT_DIAGNOSIS_FAILED",type(exc).__name__,file=sys.stderr);raise SystemExit(2)
