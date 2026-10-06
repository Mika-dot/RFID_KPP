"""Upgrade an initial passive Ubuntu checkout after checking and saving EOL changes."""
import argparse
import errno
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path


def paths(raw):
    return [os.fsdecode(p) for p in raw.split(b"\0") if p]


def checkout_plan(source, target, git):
    if git("diff", "--cached", "--name-only", "-z"):
        raise ValueError("Staged changes require review")
    if git("ls-files", "--others", "--exclude-standard", "-z"):
        raise ValueError("Untracked files require review")
    git("merge-base", "--is-ancestor", "HEAD", target)
    plan = []
    for name in paths(git("diff", "--name-only", "-z")):
        path = source / name
        if not stat.S_ISREG(path.lstat().st_mode) or not path.resolve().is_relative_to(source):
            raise ValueError("Non-regular source requires review: " + name)
        changes = git("diff", "--raw", "--", name).split(b"\t", 1)[0].split()
        if len(changes) != 5 or changes[0][1:] != changes[1] or changes[4] != b"M":
            raise ValueError("Source mode/deletion requires review: " + name)
        original = git("show", "HEAD:" + name)
        current = path.read_bytes()
        canonical = original.replace(b"\r\n", b"\n")
        if current.replace(b"\r\n", b"\n") != canonical:
            raise ValueError("Content changes require review: " + name)
        if git("show", target + ":" + name) != canonical:
            raise ValueError("Target changes content of a dirty file: " + name)
        plan.append((name, current, path.stat()))
    return plan


def save_checkout(source, backup, plan):
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content, info in plan:
        path = backup / "files" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.chmod(path, stat.S_IMODE(info.st_mode))
    manifest = [{"path": name, "sha256": hashlib.sha256(content).hexdigest()}
                for name, content, _ in plan]
    (backup / "files.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def merge_checkout(source, target, git, plan):
    for name, content, _ in plan:
        if (source / name).read_bytes() != content:
            raise ValueError("Source changed after inspection: " + name)
    if plan:
        git("add", "--renormalize", "--", *(name for name, _, _ in plan))
        staged = set(paths(git("diff", "--cached", "--name-only", "-z")))
        if staged != {name for name, _, _ in plan}:
            raise ValueError("Unexpected normalization result")
        for name in staged:
            if git("show", ":" + name) != git("show", target + ":" + name):
                raise ValueError("Normalized index differs from target: " + name)
    git("merge", "--ff-only", target)
    if git("status", "--porcelain") or git("rev-parse", "HEAD").decode().strip() != target:
        raise ValueError("Checkout is not clean at the requested release")


def stop_guardian(cgroup_root=Path("/sys/fs/cgroup")):
    unit = "perimeter-guardian.service"
    subprocess.run(["systemctl", "stop", unit], timeout=35)
    raw = subprocess.check_output([
        "systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState",
        "-p", "MainPID", "-p", "ControlPID", "-p", "ControlGroup"
    ], text=True, timeout=10)
    info = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if (info.get("LoadState") != "loaded" or info.get("ActiveState") not in ("inactive", "failed")
            or info.get("MainPID") != "0" or info.get("ControlPID") != "0"):
        raise ValueError("Guardian unit processes must be stopped before updating")
    root = cgroup_root.resolve()
    if not (root / "cgroup.controllers").is_file():
        raise ValueError("A cgroup v2 process check is required")
    groups = {"/system.slice/" + unit}
    if info.get("ControlGroup"):
        groups.add(info["ControlGroup"])
    for name in groups:
        group = (root / name.lstrip("/")).resolve()
        if group == root or not group.is_relative_to(root):
            raise ValueError("Unexpected Guardian cgroup")
        if group.exists():
            try:
                events = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
            except FileNotFoundError:
                if group.exists():
                    raise ValueError("Guardian cgroup check is unavailable")
            else:
                if events.get("populated") != "0":
                    raise ValueError("Guardian child processes remain in the unit cgroup")
    for port in (18101, 18102, 18103, 18104, 18105, 18200):
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", port)) != errno.ECONNREFUSED:
                raise ValueError("A Guardian unit port is still occupied")
    if info["ActiveState"] == "failed":
        subprocess.run(["systemctl", "reset-failed", unit], check=True, timeout=10)
    active = subprocess.check_output([
        "systemctl", "show", unit, "-p", "ActiveState", "--value"
    ], text=True, timeout=10).strip()
    if active != "inactive":
        raise ValueError("Guardian stopped state was not confirmed")
    print("STOP_VERIFIED", flush=True)


def agent_sockets():
    import psutil
    boot = int(subprocess.check_output([
        "systemctl", "show", "perimeter-guardian.service", "-p", "MainPID", "--value"
    ], text=True, timeout=10))
    agents = []
    for process in psutil.Process(boot).children():
        command = process.cmdline()
        if any(command[i:i+2] == ["-m", "guardian"] for i in range(len(command)-1)):
            agents.append(process)
    if len(agents) != 1:
        raise ValueError("Expected one Guardian agent child")
    agent = agents[0]
    connections = agent.net_connections(kind="inet")
    return {"pid": agent.pid, "created": agent.create_time(),
            "open_fds": agent.num_fds(),
            "sql_1433_sockets": sum(bool(c.raddr and c.raddr.port == 1433) for c in connections)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True)
    parser.add_argument("--node", choices=("perimetr", "comparator"), required=True)
    args = parser.parse_args(argv)
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Run this initial VM upgrade through sudo on Ubuntu")
    if len(args.release) != 40 or any(c not in "0123456789abcdef" for c in args.release):
        raise ValueError("An exact commit SHA is required")
    source = Path("/opt/perimeter/source").resolve()
    # Configure this fresh upgrade process even when the old release still pools.
    import pyodbc
    pyodbc.pooling = False
    sys.path[:0] = [str(source), str(source / "deploy/ha")]
    from environment_tool import read_generated_environment
    from guardian.config import atomic_json
    from guardian.probes import get_json
    from guardian.sql import SqlStore
    os.environ.update(read_generated_environment("/etc/perimeter/environment"))
    cfg = json.loads(Path("/etc/perimeter/node.json").read_text(encoding="utf-8-sig"))
    record = Path(cfg["state_dir"]) / "release.json"
    state = json.loads(record.read_text(encoding="utf-8-sig"))
    if cfg["node_id"] != args.node or Path(cfg["root"]).resolve() != source:
        raise ValueError("Unexpected VM identity or installation root")
    if (Path(state["current"]["root"]).resolve() != source or state.get("pending")
            or state.get("previous") is not None):
        raise ValueError("Separate release or release trial requires review")

    def git(*arguments):
        return subprocess.check_output(["runuser", "-u", "perimeter", "--", "git", "-C",
                                        str(source), *arguments], timeout=90)

    def disabled():
        lease = SqlStore().lease()
        if lease["enabled"] or lease["owner"] is not None:
            raise ValueError("HA must remain disabled throughout this initial upgrade")

    disabled()
    old = git("rev-parse", "HEAD").decode().strip()
    if state["current"]["sha"] != old:
        raise ValueError("Release record differs from checkout")
    origin = git("remote", "get-url", "origin").decode().strip().removesuffix(".git")
    if origin != "https://github.com/Mika-dot/RFID_KPP":
        raise ValueError("Unexpected source repository")
    git("fetch", "origin", args.release)
    plan = checkout_plan(source, args.release, git)
    backup = Path(cfg["state_dir"]) / "upgrade-backups" / uuid.uuid4().hex
    save_checkout(source, backup, plan)
    shutil.copy2(record, backup / "release.json")
    print("UPGRADE_PRECHECK_OK", args.node, "eol_files=" + str(len(plan)),
          "backup=" + str(backup), flush=True)
    disabled()
    stop_guardian()
    time.sleep(20)
    disabled()
    merge_checkout(source, args.release, git, plan)
    info = record.stat()
    state["current"]["sha"] = args.release
    atomic_json(record, state)
    os.chown(record, info.st_uid, info.st_gid)
    os.chmod(record, stat.S_IMODE(info.st_mode))
    disabled()
    subprocess.run(["systemctl", "start", "perimeter-guardian"], check=True, timeout=35)
    time.sleep(15)
    identity = None
    for sample in range(3):
        disabled()
        code, status = get_json("http://127.0.0.1:18200/status",
                                os.environ["PERIMETER_HA_TOKEN"], timeout=5)
        fields = ("node", "active", "healthy", "prepared", "faulted", "sample_age",
                  "release_sha", "resources")
        agent = agent_sockets()
        current_identity = (agent["pid"], agent["created"])
        if identity is not None and current_identity != identity:
            raise ValueError("Guardian restarted during the resource observation")
        identity = current_identity
        print(json.dumps({"sample": sample, "http": code,
                          **{k: status.get(k) for k in fields}, "agent": agent}), flush=True)
        if (code != 200 or status.get("node") != args.node or status.get("active") is not False
                or status.get("faulted") is not False or status.get("release_sha") != args.release):
            raise ValueError("Updated agent did not confirm its passive release")
        if sample < 2:
            time.sleep(60)
    resources = status.get("resources", {})
    if (not status.get("prepared") or status.get("sample_age", 999) >= 10
            or not isinstance(resources.get("open_fds"), int) or resources.get("restart_required")):
        raise ValueError("VM readiness/resource check failed")
    print("VM_UPDATED_READY", args.node, flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Do not include ODBC connection strings or private environment values in output.
        if isinstance(exc, ValueError):
            print("UPGRADE_FAILED", str(exc), file=sys.stderr, flush=True)
        else:
            print("UPGRADE_FAILED", type(exc).__name__, file=sys.stderr, flush=True)
        raise SystemExit(2)
