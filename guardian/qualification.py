"""Installed, offline release qualification. Candidate cannot replace its oracle."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def classify(paths):
    kinds = set()
    for path in paths:
        p = Path(path)
        if p.suffix.lower() in {".md", ".rst"}:
            kinds.add("DOCS_ONLY")
        elif path.startswith("migrations/") or p.suffix == ".sql":
            kinds.add("DB_SCHEMA")
        elif "requirements" in p.name or p.name in {"pyproject.toml", "poetry.lock", "uv.lock", "Pipfile.lock"}:
            kinds.add("DEPENDENCIES")
        elif p.suffix.lower() in {".dll", ".pt", ".onnx", ".jpg", ".png"}:
            kinds.add("MODEL_OR_DLL")
        elif path.startswith(("guardian/", "gateway/", "deploy/ha/")) or path == "common/replicated_ingest.py":
            kinds.add("HA_GUARDIAN")
        elif path.startswith("observer/"):
            kinds.add("MONITORING_ONLY")
        else:
            kinds.add("BUSINESS_LOGIC")
    if len(kinds) > 1:
        kinds.discard("DOCS_ONLY")
    return sorted(kinds or {"DOCS_ONLY"})


def offline_env():
    # Keep interpreter/OS configuration; remove destinations and credentials.
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}
    env = {k:v for k,v in os.environ.items() if k.upper() in allowed}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


def mechanical(root):
    required = ("guardian/__main__.py", "guardian/sql.py", "guardian/runtime_contract.json",
        "deploy/monitored_rfid_recovery_v2.py", "deploy/monitored_rusguard.py", "deploy/monitored_yolo.py",
        "deploy/monitored_aggregator.py", "web/kpp_reel_dashboard_v3_fixed.py")
    if any(not (root/p).is_file() for p in required):
        raise RuntimeError("CandidateEntrypointMissing")
    count = 0
    for folder in ("common", "guardian", "deploy", "RFID_reader_v4", "RTSP", "KPP", "DB_RusGard", "web", "observer", "gateway"):
        for path in (root/folder).rglob("*.py"):
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError("CandidateSourceOutsideTree")
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path.relative_to(root)))
            count += 1
    sql = ast.parse((root/"guardian/sql.py").read_text(encoding="utf-8"))
    constants = {n.targets[0].id:n.value.value for n in sql.body if isinstance(n, ast.Assign)
        and len(n.targets)==1 and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Constant)}
    if constants.get("FENCING_PROTOCOL") != 2 or constants.get("EPOCH_BARRIER") != "Perimeter.HA.Epoch":
        raise RuntimeError("CandidateFencingProtocolIncompatible")
    return count


def destination_contract(root):
    """Configuration expressions that select business destinations are protected.

    Changing an env key/default/table is an operator rollout, even when a
    candidate-owned suite is green. Runtime config is independently checked by
    preflight against the existing SQL database.
    """
    result={}
    names={"DB_CONN","CONN_STR","DB_TABLE","RFID_TABLE","SKUD_TABLE","TASK_TABLE",
           "WAREHOUSE_TABLE","EVENT_TABLE","STATE_TABLE","ACTIVE_TABLE","SRC_CONN","DST_CONN","DEST_TABLE"}
    for relative in ("RFID_reader_v4/rfid_to_sql_v4.py","RTSP/RTSP_yolo_DB_v3.py","KPP/kpp_aggregator_v3.py",
                     "KPP/kpp_aggregator_v3_warehouse_v2.py","KPP/kpp_aggregator_v3_warehouse_v3.py","DB_RusGard/db_sync_v2.py","web/kpp_reel_dashboard_v3_ru.py"):
        path=root/relative
        if not path.is_file():continue
        tree=ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node,ast.Assign):
                for target in node.targets:
                    if isinstance(target,ast.Name) and target.id in names:
                        result[relative+":"+target.id]=ast.dump(node.value,include_attributes=False)
            elif relative.startswith("DB_RusGard/") and isinstance(node,ast.FunctionDef) and node.name=="conn_str":
                result[relative+":conn_str"]=ast.dump(node,include_attributes=False)
        connect_calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
            and isinstance(node.func.value,ast.Name) and node.func.value.id=="pyodbc" and node.func.attr=="connect"]
        result[relative+":connect_calls"]=json.dumps([ast.dump(n,include_attributes=False) for n in connect_calls])
    return result


def run_replay(root, traces, output):
    result = subprocess.run([sys.executable, "-I", str(ROOT/"guardian/replay.py"), "--root", str(root),
        "--traces", str(traces), "--output", str(output)], env=offline_env(), cwd=output.parent,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    if result.returncode:
        raise RuntimeError("CandidateReplayFailed")
    return json.loads(output.read_text(encoding="utf-8"))


def qualify(candidate, stable, traces=None, allowlist=None):
    parsed = mechanical(candidate)
    if destination_contract(candidate)!=destination_contract(stable):
        raise RuntimeError("CandidateBusinessDestinationChanged")
    adapters = subprocess.run([sys.executable,"-I",str(ROOT/"guardian/adapter_checks.py"),"--root",str(candidate)],
        env=offline_env(), cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=45)
    if adapters.returncode:
        raise RuntimeError("CandidateAdapterContractFailed")
    model = subprocess.run([sys.executable,"-I",str(ROOT/"guardian/simulation.py"),"--root",str(candidate)],
        env=offline_env(), cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=30)
    if model.returncode:
        raise RuntimeError("CandidateHaSimulationFailed")
    traces = Path(traces) if traces else ROOT/"guardian/golden_traces.json"
    corpus = json.loads(traces.read_text(encoding="utf-8"))
    if corpus.get("version") != 1 or not corpus.get("traces"):
        raise RuntimeError("QualificationCorpusInvalid")
    with tempfile.TemporaryDirectory(prefix="perimeter-replay-") as tmp:
        folder = Path(tmp)
        current = run_replay(candidate, traces, folder/"candidate.json")
        before = run_replay(stable, traces, folder/"stable.json")
    expected = {t["name"]:t["expected"] for t in corpus["traces"]}
    if current != expected:
        raise RuntimeError("CandidateGoldenMismatch")
    # Approvals describe exact old/new values, not wildcard exemptions.
    allowed = json.loads(Path(allowlist).read_text(encoding="utf-8")) if allowlist else []
    differences = []
    for name in sorted(current):
        for field in current[name]:
            old, new = before.get(name, {}).get(field), current[name][field]
            if old != new:
                diff = {"trace":name, "field":field, "old":old, "new":new}
                differences.append(diff)
                if diff not in allowed:
                    raise RuntimeError("CandidateUnapprovedShadowDifference")
    return {"status":"passed", "python_files_parsed":parsed, "adapter_contracts":6, "ha_model_scenarios":8, "golden_traces":len(current),
            "shadow_differences":len(differences), "trace_sha256":hashlib.sha256(traces.read_bytes()).hexdigest(),
            "mode":"offline_recorded_inputs_no_production_connections"}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--candidate", required=True, type=Path)
    p.add_argument("--stable", required=True, type=Path)
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--traces", type=Path)
    p.add_argument("--allowlist", type=Path)
    a = p.parse_args(argv)
    try:
        report = qualify(a.candidate.resolve(), a.stable.resolve(), a.traces, a.allowlist)
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        print("QUALIFICATION_FAILED", type(exc).__name__)
        return 2
    print("QUALIFICATION_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
