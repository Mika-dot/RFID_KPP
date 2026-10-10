"""Exact release installation on the existing Windows/Linux HA nodes.

Prepare never stops services. Apply preserves the stable bootstrap and all data
paths, uses the native supervisor and ordinary HA handoff, and restores previous
configuration/release on failure. No direct SQL mutation or schema migration.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY_BASELINES = {
    "76778494851d03c5c3c5650b40e54986ddcfddd1": "3b047253645ed6f35439cff67a85a47e115417ce",
    "1c7930912ad84e8f205cf16fbdd715c76c9eb74e": "eb3dbddd5f73c3fc7c32fa53e14e612ee96d3cd5",
}
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy/ha"))
from guardian.config import atomic_json
from guardian.net import json_request
from guardian.qualification import offline_env
from guardian.release_contract import MANIFEST, validate_contract, raise_floor
from deploy.ha.prepare_resilience_config import prepare as prepare_config


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fresh_node(row, own):
    return (row.get("node") == own and type(row.get("sample_age")) in (int, float)
        and 0 <= row["sample_age"] <= 10 and type(row.get("fencing_protocol")) is int
        and row["fencing_protocol"] == 2)


def run(argv, timeout=60, env=None, cwd=None):
    result = subprocess.run([str(x) for x in argv], stdin=subprocess.DEVNULL,
        capture_output=True, timeout=timeout, env=env, cwd=cwd)
    if result.returncode:
        raise RuntimeError("DeploymentCommandFailed:" + Path(str(argv[0])).name)
    return result.stdout.decode("utf-8", "replace").strip()


def write_private(path, value, template=None):
    info = Path(template or path).stat() if Path(template or path).exists() else None
    atomic_json(path, value)
    Path(path).chmod(0o600)
    if os.name != "nt" and info:
        os.chown(path, info.st_uid, info.st_gid)


def load_environment(args, cfg):
    if os.name == "nt":
        from windows_tool import read_bundle, windows_environment
        values = windows_environment(read_bundle(args.environment))
    else:
        from environment_tool import read_generated_environment
        values = read_generated_environment(args.environment)
    values.update(cfg.get("env", {}))
    if len(values.get("PERIMETER_HA_TOKEN", "")) < 32:
        raise ValueError("ExistingClusterCredentialsRequired")
    return values


def inventory(cfg, token, request=json_request, allow_degraded=False):
    rows = {}
    for node in cfg["nodes"]:
        code, data = request(node["url"].rstrip("/") + "/status", token, timeout=3)
        if (code != 200 or data.get("node") != node["id"] or type(data.get("fencing_protocol")) is not int
                or data["fencing_protocol"] != 2 or type(data.get("sample_age")) not in (int,float)
                or not 0 <= data["sample_age"] <= 10):
            raise RuntimeError("ClusterInventoryUnavailable:" + node["id"])
        rows[node["id"]] = data
    active = [key for key, value in rows.items() if value.get("active") is True]
    if (len(active) > 1 or (not allow_degraded and
            (len(active) != 1 or rows[active[0]].get("healthy") is not True))):
        raise RuntimeError("HealthySingleOwnerRequired")
    epoch = next(iter(rows.values())).get("epoch")
    if type(epoch) is not int or any(row.get("epoch") != epoch for row in rows.values()):
        raise RuntimeError("ClusterEpochInconsistent")
    return rows, active[0] if active else None


def require_reserves(rows, own, release):
    for key in ("perimetr", "comparator"):
        if key == own:
            continue
        row = rows[key]
        if (row.get("release_sha") != release or row.get("prepared") is not True
                or row.get("faulted") is not False or row.get("operator_maintenance") is not False):
            raise RuntimeError("QualifiedReserveRequired:" + key)


def native(action):
    if action not in {"stop", "start"}:
        raise ValueError("InvalidNativeAction")
    if os.name == "nt":
        verb = "Stop" if action == "stop" else "Start"
        run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "$ErrorActionPreference='Stop'; " + verb + "-ScheduledTask -TaskName 'PerimeterGuardian'"], 40)
    else:
        run(["systemctl", action, "perimeter-guardian.service"], 40)


def git(cfg, *args):
    prefix = ["git"] if os.name == "nt" else ["runuser", "-u", "perimeter", "--", "git"]
    return run(prefix + ["-C", cfg["update_source"], *args], timeout=120)


def candidate_config(cfg, env, runtime_root=None):
    original = copy.deepcopy(cfg)
    overrides = original.setdefault("env", {})
    # Workers run in release.json's current root. Freeze old relative spool/lock
    # locations there before changing the worktree; never create a replacement queue.
    for key in ("RFID_SPOOL_PATH", "RFID_VIDEO_SPOOL", "RFID_READER_LOCK_FILE", "RFID_VIDEO_LOCK_FILE"):
        location = overrides.get(key) or env.get(key)
        if location and not Path(location).is_absolute():
            overrides[key] = str((Path(runtime_root or cfg["root"]) / location).resolve())
    value = prepare_config(original, env)
    value["auto_update"] = False  # main is changed only after this rollout is accepted.
    value["env"].update(KPP_ADAPTIVE_WINDOWS_ENABLED="1", KPP_ADAPTIVE_WINDOWS_MODE="shadow")
    # Guardian itself, not only workers, needs the separately provisioned read-only sources.
    for key in ("PERIMETER_OBSERVER_SQL", "PERIMETER_OBSERVER_TASK_SQL", "KPP_TASK_CONN_STR"):
        if env.get(key):
            value["env"][key] = env[key]
    return value


def transition_allowlist(cfg, record, source):
    """Only the two screenshot baselines admit three known warehouse fixes.

    Unknown/modified installed trees retain zero-difference qualification.
    Candidate Golden outputs must still match every expected field.
    """
    sha = record["current"]["sha"]
    if sha not in LEGACY_BASELINES:
        return []
    if (git(cfg, "-C", str(source), "rev-parse", "HEAD") != sha
            or git(cfg, "-C", str(source), "rev-parse", "HEAD^{tree}") != LEGACY_BASELINES[sha]
            or git(cfg, "-C", str(source), "status", "--porcelain", "--untracked-files=no")):
        raise RuntimeError("InstalledLegacyBaselineChanged")
    return ["--allowlist", ROOT / "guardian/legacy_shadow_allowlist.json"]


def prepare(args, cfg, env):
    record_path = Path(cfg["state_dir"]) / "release.json"
    record = json.loads(record_path.read_text(encoding="utf-8-sig"))
    if record.get("pending"):
        raise RuntimeError("ExistingTrialMustFinish")
    source = Path(record["current"]["root"])
    if git(cfg, "remote", "get-url", "origin").removesuffix(".git") != "https://github.com/Mika-dot/RFID_KPP":
        raise RuntimeError("UnexpectedSourceRepository")
    git(cfg, "fetch", "origin", args.release)
    candidate = Path(cfg["release_dir"]) / args.release
    if not candidate.exists():
        git(cfg, "worktree", "add", "--detach", str(candidate), args.release)
    if git(cfg, "-C", str(candidate), "rev-parse", "HEAD") != args.release or git(cfg, "-C", str(candidate), "status", "--porcelain"):
        raise RuntimeError("CleanExactCandidateRequired")
    manifest = (candidate / MANIFEST).read_text(encoding="utf-8")
    validate_contract(manifest, raise_floor(record.get("runtime_contract_floor")))
    # Both the installed oracle and current admission checks must accept it.
    folder = Path(cfg["state_dir"]) / "prepared" / args.release
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = folder / "qualification.json"
    oracle = source / "guardian/qualification.py"
    if not oracle.is_file():
        oracle = ROOT / "guardian/qualification.py"
    run([cfg["python"], "-I", oracle, "--candidate", candidate, "--stable", source, "--report", report,
         *transition_allowlist(cfg, record, source)], 180, offline_env())
    run([cfg["python"], "-I", ROOT / "guardian/release_contract.py", candidate], 60, offline_env())
    run([cfg["python"], "-m", "unittest", "discover", "-s", candidate / "tests"], 600,
        offline_env(), cwd=candidate)
    value = candidate_config(cfg, env, source)
    write_private(folder / "node.json", value, args.config)
    receipt = {"release":args.release, "root":str(candidate), "base":record["current"],
        "config_digest":digest(args.config), "record_digest":digest(record_path),
        "candidate_config_digest":digest(folder / "node.json"), "environment_digest":digest(args.environment),
        "tree":git(cfg,"-C",str(candidate),"rev-parse","HEAD^{tree}"),
        "qualified":True, "prepared_at":time.time(), "installed":False}
    write_private(folder / "receipt.json", receipt, args.config)
    return {"prepared":True, "release":args.release, "node":cfg["node_id"],
            "physical_acceptance":"not_measured", "services_changed":False}


def check_prepared(args, cfg):
    folder = Path(cfg["state_dir"]) / "prepared" / args.release
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    record_path = Path(cfg["state_dir"]) / "release.json"
    if (receipt.get("release") != args.release or receipt.get("qualified") is not True
            or receipt.get("config_digest") != digest(args.config)
            or receipt.get("record_digest") != digest(record_path)
            or receipt.get("environment_digest") != digest(args.environment)
            or receipt.get("candidate_config_digest") != digest(folder / "node.json")):
        raise RuntimeError("PreparedDeploymentInputsChanged")
    if git(cfg,"-C",receipt["root"],"rev-parse","HEAD^{tree}") != receipt["tree"] or git(cfg,"-C",receipt["root"],"status","--porcelain"):
        raise RuntimeError("PreparedCandidateChanged")
    return folder, receipt


def wait_handoff(cfg, token, own, release, request=json_request, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        rows = []
        for node in cfg["nodes"]:
            if node["id"] == own:
                continue
            try:
                code, row = request(node["url"].rstrip("/") + "/status",token,timeout=3)
                if (code == 200 and row.get("node") == node["id"]
                        and type(row.get("sample_age")) in (int,float) and 0 <= row["sample_age"] <= 10
                        and type(row.get("fencing_protocol")) is int and row["fencing_protocol"] == 2):
                    rows.append(row)
            except Exception:
                pass
        active = [row for row in rows if row.get("active") is True]
        if (len(rows) == 2 and len(active) == 1 and active[0].get("healthy") is True
                and active[0].get("release_sha") == release and active[0].get("update",{}).get("pending") is False
                and type(active[0].get("epoch")) is int and rows[0].get("epoch") == rows[1].get("epoch")):
            return active[0]
        if time.monotonic() >= deadline:
            raise RuntimeError("QualifiedHandoffNotConfirmed")
        time.sleep(2)


def restore(args, rollback):
    """Restore a stopped node from its private crash journal, without SQL writes."""
    from common.single_instance import SingleInstanceLock
    from guardian.processes import Processes
    cfg = rollback["config"]
    state = Path(cfg["state_dir"])
    native("stop")
    with SingleInstanceLock(str(state / "guardian.lock")):
        Processes(cfg).reap_orphans()
        write_private(args.config, cfg, state)
        write_private(state / "release.json", rollback["record"], state)
        write_private(state / "operator-maintenance.json", rollback["maintenance"], state)
    native("start")


def recover(args, cfg, env):
    journal_path = Path(cfg["state_dir"]) / "operator-release-install.json"
    rollback = json.loads(journal_path.read_text(encoding="utf-8"))
    current = json.loads((Path(cfg["state_dir"]) / "release.json").read_text(encoding="utf-8"))
    if (rollback.get("node") != cfg["node_id"] or rollback.get("release") != args.release
            or rollback.get("config_path") != str(args.config.resolve())
            or rollback["config"]["state_dir"] != cfg["state_dir"]
            or digest(args.config) not in (rollback["old_config_digest"], rollback["candidate_config_digest"])
            or current["current"]["sha"] not in (rollback["record"]["current"]["sha"], args.release)):
        raise RuntimeError("InstallJournalDoesNotMatchCurrentNode")
    restore(args, rollback)
    journal_path.unlink()  # Keep the journal if any restoration step fails.
    return {"restored": True, "node": cfg["node_id"], "release": rollback["record"]["current"]["sha"],
            "runtime_acceptance": "ordinary_guardian_gate"}


def apply(args, cfg, env):
    from common.single_instance import SingleInstanceLock
    from guardian.processes import Processes
    folder, receipt = check_prepared(args,cfg)
    token, own = env["PERIMETER_HA_TOKEN"], cfg["node_id"]
    recovery = getattr(args, "recover_reserve", False)
    rows, owner = inventory(cfg,token, allow_degraded=recovery)
    if recovery and (own not in {"perimetr", "comparator"} or own == owner or args.handoff):
        raise RuntimeError("RecoveryRequiresPassiveLinuxReserve")
    if rows[own].get("release_sha") != receipt["base"]["sha"]:
        raise RuntimeError("InstalledReleaseChanged")
    if own == "physical" or own == owner:
        require_reserves(rows,own,args.release)
    if own == owner and not args.handoff:
        raise RuntimeError("ActiveNodeRequiresHandoffMode")
    state = Path(cfg["state_dir"])
    journal_path = state / "operator-release-install.json"
    if journal_path.exists():
        raise RuntimeError("PreviousInstallJournalRequiresInspection")
    record_path, maintenance = state / "release.json", state / "operator-maintenance.json"
    old_record = json.loads(record_path.read_text(encoding="utf-8"))
    old_maintenance = json.loads(maintenance.read_text(encoding="utf-8")) if maintenance.exists() else {"enabled":False}
    rollback = {"node":own,"release":args.release,"config":cfg,"record":old_record,
        "maintenance":old_maintenance,"installed":False,"config_path":str(args.config.resolve()),
        "old_config_digest":digest(args.config),"candidate_config_digest":digest(folder / "node.json")}
    write_private(journal_path,rollback,args.config)
    stopped = False
    try:
        if own != owner:
            code, _ = json_request(next(n["url"] for n in cfg["nodes"] if n["id"] == own).rstrip("/")+"/maintenance",
                token,body={"enabled":True,"release":receipt["base"]["sha"]},timeout=10)
            if code != 200:
                raise RuntimeError("ReserveMaintenanceNotConfirmed")
        native("stop")
        stopped = True
        # The stable launcher must be stopped; the lock rules out another Guardian.
        with SingleInstanceLock(str(state / "guardian.lock")):
            Processes(cfg).reap_orphans()
            write_private(maintenance,{"enabled":True},state)
            if own == owner:
                wait_handoff(cfg,token,own,args.release)
            new_cfg = json.loads((folder / "node.json").read_text(encoding="utf-8"))
            record = copy.deepcopy(old_record)
            record.update(previous=record["current"], current={"sha":args.release,"root":receipt["root"],"python":cfg["python"]},
                pending=True, trial_started=False, activated_at=time.time(), fencing_protocol_min=2)
            record["qualification"] = {"status":"passed","sha":args.release,"mode":"operator_exact_release"}
            write_private(args.config,new_cfg,state)
            write_private(record_path,record,state)
        native("start")
        url=next(n["url"] for n in cfg["nodes"] if n["id"] == own).rstrip("/")
        deadline=time.monotonic()+180
        while True:
            try:
                code,row=json_request(url+"/status",token,timeout=3)
                if (code==200 and fresh_node(row,own) and row.get("release_sha")==args.release and row.get("operator_maintenance") is True
                        and row.get("active") is False and row.get("preflight",{}).get("ok") is True):
                    break
            except Exception:
                pass
            if time.monotonic()>=deadline:
                raise RuntimeError("InstalledPassivePreflightFailed")
            time.sleep(2)
        code,_=json_request(url+"/maintenance",token,body={"enabled":False,"release":args.release},timeout=10)
        if code!=200:
            raise RuntimeError("InstalledMaintenanceReleaseFailed")
        deadline = time.monotonic() + 420
        while True:
            try:
                code, row = json_request(url + "/status", token, timeout=3)
                if code == 200 and fresh_node(row,own) and row.get("release_sha") != args.release:
                    raise RuntimeError("InstalledCandidateRolledBack")
                if (code == 200 and fresh_node(row,own) and row.get("prepared") is True and row.get("faulted") is False
                        and row.get("operator_maintenance") is False and row.get("replication_enabled") is True
                        and row.get("fallback_enabled") is True
                        and (row.get("active") is False or (row.get("healthy") is True and row.get("update", {}).get("pending") is False))):
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("InstalledQualificationNotConfirmed")
            time.sleep(2)
        rollback.update(installed=True,finished_at=time.time())
        write_private(folder / "installation.json",rollback,args.config)
        journal_path.unlink()
        return {"installed":True,"release":args.release,"node":own,"probation":"ordinary_guardian_gate",
                "data_paths_preserved":True,"physical_acceptance":"not_measured"}
    except Exception:
        if stopped:
            restore(args, rollback)
        elif own != owner:
            # Even a lost HTTP response may have entered maintenance successfully.
            url = next(n["url"] for n in cfg["nodes"] if n["id"] == own).rstrip("/")
            code, _ = json_request(url + "/maintenance", token,
                body={"enabled":old_maintenance.get("enabled",False),"release":receipt["base"]["sha"]},timeout=10)
            if code != 200:
                raise RuntimeError("PreviousMaintenanceRestoreFailedJournalRetained")
        journal_path.unlink(missing_ok=True)
        raise


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan",action="store_true");mode.add_argument("--prepare",action="store_true");mode.add_argument("--apply",action="store_true");mode.add_argument("--recover",action="store_true")
    p.add_argument("--release",required=True);p.add_argument("--handoff",action="store_true")
    p.add_argument("--recover-reserve", action="store_true",
                   help="Install a passive Linux reserve when no healthy owner exists; ordinary fencing/preflight remain required")
    p.add_argument("--config",type=Path,default=Path("D:/PerimeterHA/node.json" if os.name=="nt" else "/etc/perimeter/node.json"))
    p.add_argument("--environment",type=Path,default=Path("D:/PerimeterHA/transfer-private/environment.local.json" if os.name=="nt" else "/etc/perimeter/environment"))
    args=p.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}",args.release):
        raise ValueError("ExactReleaseRequired")
    cfg=json.loads(args.config.read_text(encoding="utf-8-sig"))
    if (os.name=="nt") != (cfg["node_id"]=="physical"):
        raise RuntimeError("DeploymentOperatingSystemMismatch")
    env=load_environment(args,cfg)
    if args.plan:
        result={"release":args.release,"node":cfg["node_id"],"data_paths_preserved":True,
            "order":["comparator","perimetr","physical","ub22"],"handoff_required_for_active":True,
            "changes_applied":False,"observer_dedicated_credentials":bool(env.get("PERIMETER_OBSERVER_SQL"))}
    else:
        import ctypes
        if not (bool(ctypes.windll.shell32.IsUserAnAdmin()) if os.name=="nt" else os.geteuid()==0):
            raise RuntimeError("AdministratorRequired")
        os.environ.update(env)
        from common.single_instance import SingleInstanceLock
        with SingleInstanceLock(str(Path(cfg["state_dir"]) / "operator-update.lock")):
            result=prepare(args,cfg,env) if args.prepare else recover(args,cfg,env) if args.recover else apply(args,cfg,env)
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("RELEASE_UPDATE_FAILED",str(exc) if type(exc) in (ValueError,RuntimeError) else type(exc).__name__,file=sys.stderr)
        raise SystemExit(2)
