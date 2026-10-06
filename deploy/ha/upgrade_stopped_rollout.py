"""Upgrade a stopped HA checkout after rollback; keep HA OFF and legacy running.

Download outside source and run native Python -B as Administrator/sudo. This
phase deliberately leaves the native launcher disabled. It never starts workers,
changes SQL, or modifies business state. Later cutover is a separate operation.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

BASE = "d898dbc05feeb3157304d0c34cca344514b59b38"
PRIORITY = [("physical", 1), ("perimetr", 2), ("comparator", 3)]


class Abort(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Abort(message)


def disabled_database(driver, connection):
    """Use only SELECT, not even an identity-preserving lease UPDATE."""
    conn = driver.connect(connection, timeout=5, autocommit=True)
    try:
        conn.timeout = 5
        row = conn.execute("SELECT Enabled,Owner FROM dbo.KPP_HA_Lease WHERE Id=1").fetchone()
        require(row is not None and not row[0] and row[1] is None,
                "HA must be disabled and unowned; no upgrade permitted")
    finally:
        conn.close()


def valid_record(state, source, head, release, journal):
    require(Path(state["current"]["root"]).resolve() == source
            and not state.get("pending") and state.get("previous") is None,
            "A separate release/trial is present")
    recorded = state["current"]["sha"]
    if journal is not None:
        require(journal.get("release") == release and journal.get("base") == BASE
                and journal.get("root") == str(source), "A different stopped rollout is present")
    require(recorded == head or (journal is not None and recorded == BASE and head == release),
            "Release record differs from checkout without a matching recovery journal")
    require(head in (BASE, release), "Unexpected installed release")


def windows_port_free(port):
    """Check the Windows TCP table and exclusive bind, not timed connect_ex.

    A failed/timeout connection is not evidence that a listener exists. Bind
    checks local availability directly; SO_EXCLUSIVEADDRUSE prevents sharing or
    taking over an existing listener. Never call listen and always close.
    """
    import psutil
    listeners = [c for c in psutil.net_connections(kind="tcp")
                 if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port]
    require(not listeners, "HA_PORT_LISTENING port=" + str(port) + " pids=" +
            ",".join(str(c.pid) for c in listeners))
    require(hasattr(socket, "SO_EXCLUSIVEADDRUSE"), "Windows exclusive bind check unavailable")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("0.0.0.0", port))
        except OSError as exc:
            raise Abort("HA_PORT_BIND_FAILED port=" + str(port) + " code=" +
                        str(getattr(exc, "winerror", None) or exc.errno)) from None


def native_stopped(windows, source, config):
    """Inspect native launchers and process identities, without stopping anything."""
    if windows:
        code = ("$ErrorActionPreference='Stop'; "
                "$t=Get-ScheduledTask -TaskName 'PerimeterGuardian'; "
                "@{state=[string]$t.State;enabled=[bool]$t.Settings.Enabled}|ConvertTo-Json -Compress")
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
                           capture_output=True, timeout=20)
        require(r.returncode == 0, "Cannot inspect the Guardian scheduled task")
        info = json.loads(r.stdout.decode("utf-8-sig"))
        require(info.get("state") == "Disabled" and info.get("enabled") is False,
                "Guardian scheduled task must already be disabled")
    else:
        r = subprocess.run(["systemctl", "show", "perimeter-guardian.service", "-p", "LoadState",
                            "-p", "ActiveState", "-p", "MainPID", "-p", "ControlPID",
                            "-p", "UnitFileState"], capture_output=True, text=True, timeout=10)
        require(r.returncode == 0, "Cannot inspect the Guardian systemd unit")
        info = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        require(info.get("LoadState") == "loaded" and info.get("ActiveState") == "inactive"
                and info.get("MainPID") == "0" and info.get("ControlPID") == "0"
                and info.get("UnitFileState") == "disabled", "Guardian unit must already be inactive/disabled")
        group = Path("/sys/fs/cgroup/system.slice/perimeter-guardian.service")
        require(Path("/sys/fs/cgroup/cgroup.controllers").is_file(), "cgroup v2 inspection is required")
        if group.exists():
            events = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
            require(events.get("populated") == "0", "Guardian unit still has child processes")
    import psutil
    root = str(source).replace("\\", "/").casefold().rstrip("/") + "/"
    node_config = str(config).replace("\\", "/").casefold()
    for p in psutil.process_iter():
        try:
            name = p.name().lower()
            if p.pid == os.getpid() or not (name.startswith("python") or name in (
                    "cmd.exe", "powershell.exe", "wine", "wine-preloader")):
                continue
            args = [a.replace("\\", "/").casefold() for a in p.cmdline()[1:]]
            require(not any(a.startswith(root) or a == node_config for a in args),
                    "An HA-root/config process remains; nothing was stopped")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise Abort("Cannot inspect a candidate process identity") from None
    # Legacy occupies worker ports on Windows; only the HA control port must be free.
    if windows:
        windows_port_free(18200)
        return
    for port in (18101, 18102, 18103, 18104, 18105, 18200):
        with socket.socket() as probe:
            probe.settimeout(1)
            result = probe.connect_ex(("127.0.0.1", port))
            require(result == errno.ECONNREFUSED,
                    "HA_PORT_CONNECT_UNVERIFIED port=" + str(port) + " code=" + str(result))


def main(argv=None):
    sys.dont_write_bytecode = True
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--node", choices=("physical", "perimetr", "comparator"), required=True)
    args = parser.parse_args(argv)
    require(bool(re.fullmatch(r"[0-9a-f]{40}", args.release)) and args.release != BASE,
            "An exact new release SHA is required")
    windows = os.name == "nt"
    require((windows and args.node == "physical") or (not windows and args.node != "physical"),
            "Node does not match the operating system")
    require(bool(ctypes.windll.shell32.IsUserAnAdmin()) if windows else os.geteuid() == 0,
            "Use Administrator PowerShell/sudo")
    config = Path("D:/PerimeterHA/node.json" if windows else "/etc/perimeter/node.json")
    cfg = json.loads(config.read_text(encoding="utf-8-sig"))
    source = Path(cfg["root"]).resolve()
    expected_source = Path("D:/PerimeterHA/source" if windows else "/opt/perimeter/source").resolve()
    require(source == expected_source and cfg["node_id"] == args.node, "Unexpected installation identity")
    require(sorted(((n["id"], n["priority"]) for n in cfg["nodes"]), key=lambda n: n[1]) == PRIORITY,
            "Unexpected executor priorities")
    require(cfg.get("controller_enabled") is (args.node != "physical"), "Unexpected controller role")
    require(not Path(__file__).resolve().is_relative_to(source), "Download this tool outside source")
    sys.path[:0] = [str(source), str(source / "deploy/ha")]
    if windows:
        from windows_tool import read_bundle, windows_environment
        from upgrade_initial_windows import cache_plan, save_caches, restore_caches
        os.environ.update(windows_environment(read_bundle(
            "D:/PerimeterHA/transfer-private/environment.local.json")))
    else:
        from environment_tool import read_generated_environment
        from upgrade_initial_vm import checkout_plan, save_checkout, merge_checkout
        os.environ.update(read_generated_environment("/etc/perimeter/environment"))
    from guardian.config import atomic_json
    from common.single_instance import SingleInstanceLock
    import pyodbc
    pyodbc.pooling = False

    def git(*arguments):
        prefix = ["C:/Program Files/Git/cmd/git.exe"] if windows else ["runuser", "-u", "perimeter", "--", "git"]
        r = subprocess.run(prefix + ["-C", str(source), *arguments], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=120)
        require(r.returncode == 0, "Git command failed: " + arguments[0])
        return r.stdout

    def check():
        disabled_database(pyodbc, os.environ["PERIMETER_HA_SQL"])
        native_stopped(windows, source, config)
        if windows:
            from guardian.probes import services_health
            health = services_health()
            require(len(health) == 5 and all(v.get("ok") is True for v in health.values()),
                    "Legacy readiness is not confirmed for all five services")

    state_dir = Path(cfg["state_dir"])
    record = state_dir / "release.json"
    journal_path = state_dir / "stopped-rollout.json"

    def write_state(path, value):
        info = path.stat() if path.exists() else record.stat()
        atomic_json(path, value)
        if not windows:
            os.chown(path, info.st_uid, info.st_gid)
            path.chmod(0o600)

    check()
    with SingleInstanceLock(str(state_dir / "guardian.lock")):
        check()
        state = json.loads(record.read_text(encoding="utf-8-sig"))
        head = git("rev-parse", "HEAD").decode().strip()
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
        valid_record(state, source, head, args.release, journal)
        require(git("remote", "get-url", "origin").decode().strip().removesuffix(".git") ==
                "https://github.com/Mika-dot/RFID_KPP", "Unexpected source repository")
        git("fetch", "origin", args.release)
        plan = cache_plan(source, args.release, git) if windows else checkout_plan(source, args.release, git)
        candidate = Path(cfg["release_dir"]) / ("stopped-check-" + args.release)
        if not candidate.exists():
            git("worktree", "add", "--detach", str(candidate), args.release)
        require(git("-C", str(candidate), "rev-parse", "HEAD").decode().strip() == args.release
                and not git("-C", str(candidate), "status", "--porcelain"), "Candidate checkout was modified")
        import ast
        tree = ast.parse((candidate / "guardian/sql.py").read_text(encoding="utf-8"))
        constants = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                     if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                     and n.targets[0].id in ("FENCING_PROTOCOL", "EPOCH_BARRIER")}
        require(constants == {"FENCING_PROTOCOL": 2, "EPOCH_BARRIER": "Perimeter.HA.Epoch"},
                "Candidate lacks fencing protocol 2")
        require((candidate / "migrations/004_perimeter_ha_epoch_barrier.sql").is_file(),
                "Candidate migration is missing")
        result = subprocess.run([cfg["python"], "-B", "-m", "unittest", "discover", "-s", "tests",
                                 "-p", "test_ha_runtime_*.py"], cwd=candidate,
                                stdin=subprocess.DEVNULL, timeout=60)
        require(result.returncode == 0, "Candidate runtime regression checks failed")
        check()
        require(json.loads(record.read_text(encoding="utf-8-sig")) == state,
                "Release record changed during inspection")
        require(git("rev-parse", "HEAD").decode().strip() == head, "Checkout changed during inspection")
        if journal is None:
            backup = state_dir / "upgrade-backups" / ("stopped-" + uuid.uuid4().hex)
            if windows:
                save_caches(backup, plan)
            else:
                save_checkout(source, backup, plan)
            shutil.copy2(config, backup / "node.json")
            shutil.copy2(record, backup / "release.json")
            for name in ("operator-maintenance.json", "repair-verification.json"):
                if (state_dir / name).exists():
                    shutil.copy2(state_dir / name, backup / name)
            if not windows:
                for path in backup.rglob("*"):
                    if path.is_file():
                        path.chmod(0o600)
            journal = {"base": BASE, "release": args.release, "root": str(source), "backup": str(backup)}
            write_state(journal_path, journal)
        print("STOPPED_UPGRADE_PRECHECK_OK", args.node, "backup=" + journal["backup"], flush=True)
        write_state(state_dir / "operator-maintenance.json", {"enabled": True})
        write_state(state_dir / "repair-verification.json", {"required": True})
        check()
        if windows:
            restore_caches(source, git, plan)
            git("merge", "--ff-only", args.release)
        else:
            merge_checkout(source, args.release, git, plan)
        require(git("rev-parse", "HEAD").decode().strip() == args.release
                and not git("status", "--porcelain"), "Updated checkout is not clean at the requested release")
        state["current"]["sha"] = args.release
        state["fencing_protocol_min"] = 2
        write_state(record, state)
        check()
        print("STOPPED_RUNTIME_UPDATED", args.node, args.release, flush=True)
        print("HA_DISABLED_GUARDIAN_DISABLED_LEGACY_UNTOUCHED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never print raw ODBC exceptions/connection strings/environment/argv.
        print("STOPPED_UPGRADE_ABORT", str(exc) if isinstance(exc, Abort) else type(exc).__name__, flush=True)
        raise SystemExit(2) from None
