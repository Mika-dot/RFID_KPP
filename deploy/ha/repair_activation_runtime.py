"""Apply a tested runtime hotfix to one installed initial HA node, with fencing kept enabled."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

BASE = "0c573bc708f2906c240af64a23e0f6042006a669"
PRIORITY = [("physical", 1), ("perimetr", 2), ("comparator", 3)]


class Abort(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Abort(message)


def process_alive(psutil, identity):
    try:
        return psutil.Process(identity[0]).create_time() == identity[1]
    except psutil.NoSuchProcess:
        return False


def stop_windows(config):
    import psutil
    def powershell(code):
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
                       check=True, timeout=25)
    powershell("Disable-ScheduledTask -TaskName 'PerimeterGuardian' -ErrorAction Stop | Out-Null")
    match = str(config).replace("/", "\\").casefold()
    targets = {}
    for process in psutil.process_iter():
        try:
            if process.pid == os.getpid() or process.name().lower() not in (
                    "python.exe", "pythonw.exe", "powershell.exe", "cmd.exe"):
                continue
            if any(arg.replace("/", "\\").casefold() == match for arg in process.cmdline()[1:]):
                for item in [process] + process.children(recursive=True):
                    targets[item.pid] = (item.pid, item.create_time())
        except psutil.NoSuchProcess:
            continue
    powershell("Stop-ScheduledTask -TaskName 'PerimeterGuardian' -ErrorAction Stop")
    for identity in targets.values():
        if process_alive(psutil, identity):
            subprocess.run(["taskkill.exe", "/PID", str(identity[0]), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    deadline = time.monotonic() + 15
    while any(process_alive(psutil, identity) for identity in targets.values()):
        require(time.monotonic() < deadline, "A captured Guardian process remains")
        time.sleep(.25)
    for port in (18101, 18102, 18103, 18104, 18105, 18200):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=.5):
                raise Abort("A Guardian port remains occupied")
        except OSError:
            pass
    print("STOP_VERIFIED", flush=True)


def start_guardian(windows):
    if windows:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                        "Enable-ScheduledTask -TaskName 'PerimeterGuardian' -ErrorAction Stop | Out-Null; "
                        "Start-ScheduledTask -TaskName 'PerimeterGuardian' -ErrorAction Stop"],
                       check=True, timeout=25)
    else:
        subprocess.run(["systemctl", "start", "perimeter-guardian"], check=True, timeout=35)


def quarantine(store, node):
    require(store.lease()["enabled"], "This repair requires enabled HA")
    # Ask the existing deterministic controller to vacate this node first.
    store.fault(node)
    deadline = time.monotonic() + 45
    while True:
        lease = store.lease()
        require(lease["enabled"], "HA enable state changed")
        if not (lease["valid"] and lease["owner"] == node):
            store.begin_repair(node)  # Transactionally prevents a new grant.
            print("NODE_QUARANTINED", node, flush=True)
            return
        require(time.monotonic() < deadline, "Controller did not fence this node; nothing was stopped")
        time.sleep(2)


def summarize(status):
    result = {key: status.get(key) for key in (
        "node", "active", "healthy", "prepared", "faulted", "sample_age", "release_sha")}
    result["services"] = {name: item.get("ok") for name, item in status.get("services", {}).items()}
    return result


def wait_local(get_json, token, node, release):
    deadline = time.monotonic() + 150
    last = None
    while True:
        try:
            code, status = get_json("http://127.0.0.1:18200/status", token, timeout=5)
            current = summarize(status)
            if current != last:
                print("HOTFIX_WAIT", json.dumps(current), flush=True)
                last = current
            good = (code == 200 and status.get("node") == node
                    and status.get("release_sha") == release
                    and status.get("sample_age", 999) < 10 and not status.get("faulted", True)
                    and not status.get("resources", {}).get("restart_required")
                    and (status.get("healthy") if status.get("active") else status.get("prepared")))
            if good:
                print("HOTFIX_NODE_READY", node, flush=True)
                return
        except (OSError, ValueError):
            pass
        require(time.monotonic() < deadline, "Updated node has not confirmed readiness; send this output")
        time.sleep(5)


def wait_cluster(cfg, store, get_json, token, release):
    deadline, streak, identity = time.monotonic() + 300, 0, None
    while True:
        lease = store.lease()
        rows = []
        good = lease["enabled"] and lease["valid"] and lease["owner"] == "physical"
        for peer in cfg["nodes"]:
            try:
                code, status = get_json(peer["url"].rstrip("/") + "/status", token, timeout=5)
                rows.append(summarize(status))
                good = good and (code == 200 and status.get("node") == peer["id"]
                    and status.get("release_sha") == release and status.get("sample_age", 999) < 10
                    and status.get("faulted") is False)
                if peer["id"] == "physical":
                    good = good and status.get("active") is True and status.get("healthy") is True
                    good = good and status.get("epoch") == lease["epoch"]
                    services = status.get("services", {})
                    good = good and len(services) == 5 and all(item.get("ok") is True for item in services.values())
                else:
                    good = good and status.get("active") is False and status.get("prepared") is True
            except Exception as exc:
                rows.append({"node": peer["id"], "error": type(exc).__name__})
                good = False
        with store.connect() as conn:
            row = conn.execute("SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
                               "FROM dbo.KPP_HA_Controller WHERE Id=1").fetchone()
        controller = {"owner": row[0], "valid": bool(row[1])}
        good = good and controller == {"owner": "comparator", "valid": True}
        current = (lease["owner"], lease["epoch"])
        streak = streak + 1 if good and current == identity else int(good)
        identity = current
        print("CLUSTER_WAIT", json.dumps({"lease": lease, "controller": controller,
                                           "healthy_samples": streak, "nodes": rows}), flush=True)
        if streak >= 3:
            print("HA_ACTIVE_PHYSICAL_RESERVES_READY", flush=True)
            return
        require(time.monotonic() < deadline, "Full cluster operation has not been confirmed; send this output")
        time.sleep(10)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--node", required=True, choices=("physical", "perimetr", "comparator"))
    parser.add_argument("--wait-cluster", action="store_true")
    args = parser.parse_args(argv)
    require(bool(re.fullmatch(r"[0-9a-f]{40}", args.release)), "An exact release SHA is required")
    windows = os.name == "nt"
    require((windows and args.node == "physical") or (not windows and args.node != "physical"),
            "Node does not match this operating system")
    require(bool(ctypes.windll.shell32.IsUserAnAdmin()) if windows else os.geteuid() == 0,
            "Run through Administrator PowerShell or sudo")
    config = Path("D:/PerimeterHA/node.json" if windows else "/etc/perimeter/node.json")
    cfg = json.loads(config.read_text(encoding="utf-8-sig"))
    source = Path(cfg["root"]).resolve()
    sys.path[:0] = [str(source), str(source / "deploy/ha")]
    if windows:
        from windows_tool import read_bundle, windows_environment
        from upgrade_initial_windows import cache_plan, save_caches, restore_caches
        os.environ.update(windows_environment(read_bundle(
            "D:/PerimeterHA/transfer-private/environment.local.json")))
    else:
        from environment_tool import read_generated_environment
        from upgrade_initial_vm import checkout_plan, save_checkout, merge_checkout, stop_guardian
        os.environ.update(read_generated_environment("/etc/perimeter/environment"))
    from guardian.sql import SqlStore
    from guardian.probes import get_json
    from guardian.config import atomic_json
    from common.single_instance import SingleInstanceLock
    store = SqlStore()
    token = os.environ["PERIMETER_HA_TOKEN"]
    require(cfg["node_id"] == args.node, "Unexpected configured node identity")
    require(sorted([(n["id"], n["priority"]) for n in cfg["nodes"]], key=lambda n: n[1]) == PRIORITY,
            "Unexpected executor priorities")
    record = Path(cfg["state_dir"]) / "release.json"

    def git(*arguments):
        prefix = ["C:/Program Files/Git/cmd/git.exe"] if windows else ["runuser", "-u", "perimeter", "--", "git"]
        result = subprocess.run(prefix + ["-C", str(source), *arguments],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        require(result.returncode == 0, "Git command failed: " + arguments[0])
        return result.stdout

    def initial_state():
        state = json.loads(record.read_text(encoding="utf-8-sig"))
        require(Path(state["current"]["root"]).resolve() == source
                and not state.get("pending") and state.get("previous") is None,
                "A separate release/trial is present; send this output")
        require(state["current"]["sha"] == git("rev-parse", "HEAD").decode().strip(),
                "Release record differs from checkout")
        return state

    state = initial_state()
    if state["current"]["sha"] == args.release:
        wait_local(get_json, token, args.node, args.release)
        if args.wait_cluster:
            wait_cluster(cfg, store, get_json, token, args.release)
        return 0
    require(state["current"]["sha"] == BASE, "Expected the initial runtime release")
    require(git("remote", "get-url", "origin").decode().strip().removesuffix(".git") ==
            "https://github.com/Mika-dot/RFID_KPP", "Unexpected source repository")
    git("fetch", "origin", args.release)
    plan = cache_plan(source, args.release, git) if windows else checkout_plan(source, args.release, git)
    candidate = Path(cfg["release_dir"]) / ("hotfix-check-" + args.release)
    if not candidate.exists():
        git("worktree", "add", "--detach", str(candidate), args.release)
    require(git("-C", str(candidate), "rev-parse", "HEAD").decode().strip() == args.release,
            "Unexpected test candidate")
    require(not git("-C", str(candidate), "status", "--porcelain"), "Test candidate was modified")
    tests = subprocess.run([cfg["python"], "-B", "-m", "unittest", "discover", "-s", "tests",
                            "-p", "test_ha_runtime_*.py"], cwd=candidate,
                           stdin=subprocess.DEVNULL, timeout=60)
    require(tests.returncode == 0, "Candidate regression tests failed; nothing was stopped")
    backup = Path(cfg["state_dir"]) / "upgrade-backups" / uuid.uuid4().hex
    if windows:
        save_caches(backup, plan)
    else:
        save_checkout(source, backup, plan)
    shutil.copy2(config, backup / "node.json")
    shutil.copy2(record, backup / "release.json")
    initial_state()
    print("HOTFIX_PRECHECK_OK", args.node, "backup=" + str(backup), flush=True)
    quarantine(store, args.node)
    restart = False
    try:
        restart = True
        stop_windows(config) if windows else stop_guardian()
        with SingleInstanceLock(str(Path(cfg["state_dir"]) / "guardian.lock")):
            # A previous automatic recovery can race the operator's first claim.
            # The stopped agent cannot clear this second quarantine.
            quarantine(store, args.node)
            state = initial_state()
            if windows:
                restore_caches(source, git, plan)
                git("merge", "--ff-only", args.release)
            else:
                merge_checkout(source, args.release, git, plan)
            require(git("rev-parse", "HEAD").decode().strip() == args.release
                    and not git("status", "--porcelain"), "Updated checkout was not verified")
            def write_record(path, value):
                info = path.stat() if path.exists() else Path(cfg["state_dir"]).stat()
                atomic_json(path, value)
                if not windows:
                    os.chown(path, info.st_uid, info.st_gid)
                    path.chmod(0o600)
            state["current"]["sha"] = args.release
            write_record(record, state)
            write_record(Path(cfg["state_dir"]) / "repair-verification.json", {"required": True})
            print("HOTFIX_INSTALLED", args.node, args.release, flush=True)
    finally:
        if restart:
            start_guardian(windows)
    wait_local(get_json, token, args.node, args.release)
    if args.wait_cluster:
        wait_cluster(cfg, store, get_json, token, args.release)
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("HOTFIX_FAILED", str(exc) if isinstance(exc, Abort) else type(exc).__name__, flush=True)
        raise SystemExit(2)
