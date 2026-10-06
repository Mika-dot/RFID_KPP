"""Start the pinned protocol-2 agents under persistent maintenance with HA OFF.

This phase never stops legacy, changes SQL, releases maintenance, or enables HA.
Windows scheduled task must be enabled to run it; Linux autostart stays disabled.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RELEASE = "79d1caa3a0709ca96d6ee6d55c8ed73623ca8665"
PRIORITY = [("physical", 1), ("perimetr", 2), ("comparator", 3)]


class Abort(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Abort(message)


def check_record(cfg, source):
    state = Path(cfg["state_dir"])
    record = json.loads((state / "release.json").read_text(encoding="utf-8-sig"))
    require(record["current"]["sha"] == RELEASE
            and Path(record["current"]["root"]).resolve() == source
            and record.get("fencing_protocol_min") == 2
            and record.get("pending") is False and record.get("previous") is None,
            "The pinned stopped upgrade must be completed first")
    require(json.loads((state / "operator-maintenance.json").read_text()).get("enabled") is True,
            "Persistent operator maintenance must already be enabled")
    require(json.loads((state / "repair-verification.json").read_text()).get("required") is True,
            "Independent repair verification must remain required")


def staged(code, status, node):
    return (code == 200 and status.get("node") == node and status.get("release_sha") == RELEASE
            and status.get("fencing_protocol") == 2 and status.get("sample_age", 999) < 10
            and status.get("active") is False and status.get("operator_maintenance") is True
            and not status.get("resources", {}).get("restart_required", False))


def summary(status):
    fields = ("node", "release_sha", "fencing_protocol", "operator_maintenance",
              "active", "healthy", "prepared", "faulted", "sample_age", "preflight")
    return {key: status.get(key) for key in fields}


def main(argv=None):
    sys.dont_write_bytecode = True
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", choices=("physical", "perimetr", "comparator"), required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--start", action="store_true")
    modes.add_argument("--cluster-check", action="store_true")
    args = parser.parse_args(argv)
    windows = os.name == "nt"
    require((windows and args.node == "physical") or (not windows and args.node != "physical"),
            "Node does not match operating system")
    require(bool(ctypes.windll.shell32.IsUserAnAdmin()) if windows else os.geteuid() == 0,
            "Use Administrator PowerShell/sudo")
    source = Path("D:/PerimeterHA/source" if windows else "/opt/perimeter/source").resolve()
    config = Path("D:/PerimeterHA/node.json" if windows else "/etc/perimeter/node.json")
    cfg = json.loads(config.read_text(encoding="utf-8-sig"))
    require(Path(cfg["root"]).resolve() == source and cfg["node_id"] == args.node,
            "Unexpected installation identity")
    require(sorted(((n["id"], n["priority"]) for n in cfg["nodes"]), key=lambda n: n[1]) == PRIORITY
            and cfg.get("controller_enabled") is (args.node != "physical"), "Unexpected controller roles")
    check_record(cfg, source)
    sys.path[:0] = [str(source), str(source / "deploy/ha")]
    if windows:
        from windows_tool import read_bundle, windows_environment
        os.environ.update(windows_environment(read_bundle(
            "D:/PerimeterHA/transfer-private/environment.local.json")))
    else:
        from environment_tool import read_generated_environment
        os.environ.update(read_generated_environment("/etc/perimeter/environment"))
    from upgrade_stopped_rollout import Abort as StoppedAbort, disabled_database, native_stopped
    from guardian.probes import get_json, services_health
    from guardian.sql import FENCING_PROTOCOL
    require(FENCING_PROTOCOL == 2, "Installed SQL code does not implement protocol 2")
    import pyodbc
    pyodbc.pooling = False
    token = os.environ["PERIMETER_HA_TOKEN"]

    def disabled():
        try:
            disabled_database(pyodbc, os.environ["PERIMETER_HA_SQL"])
        except StoppedAbort as exc:
            raise Abort(str(exc)) from None

    def legacy_ready():
        if windows:
            health = services_health()
            require(len(health) == 5 and all(v.get("ok") is True for v in health.values()),
                    "Five legacy services must remain ready")

    prefix = ["C:/Program Files/Git/cmd/git.exe"] if windows else ["runuser", "-u", "perimeter", "--", "git"]
    r = subprocess.run(prefix + ["-C", str(source), "rev-parse", "HEAD"],
                       capture_output=True, timeout=15)
    require(r.returncode == 0 and r.stdout.decode().strip() == RELEASE, "Checkout differs from release record")
    disabled()
    legacy_ready()
    if args.start:
        # An existing matching agent makes this command safe to repeat.
        try:
            code, status = get_json("http://127.0.0.1:18200/status", token, timeout=3)
        except (OSError, ValueError):
            code, status = None, {}
        if code is not None:
            require(staged(code, status, args.node), "An incompatible/unpaused agent is already running")
        else:
            try:
                native_stopped(windows, source, config)
            except StoppedAbort as exc:
                raise Abort(str(exc)) from None
            check_record(cfg, source)
            disabled()
            if windows:
                command = ("$ErrorActionPreference='Stop'; "
                           "Enable-ScheduledTask -TaskName 'PerimeterGuardian' | Out-Null; "
                           "Start-ScheduledTask -TaskName 'PerimeterGuardian'")
                r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                   capture_output=True, timeout=25)
            else:
                r = subprocess.run(["systemctl", "start", "perimeter-guardian"],
                                   capture_output=True, timeout=35)
            require(r.returncode == 0, "Native Guardian start failed")
            print("GUARDIAN_START_REQUESTED", args.node, flush=True)
        deadline, streak, reported = time.monotonic() + 150, 0, 0
        while True:
            disabled()
            try:
                code, status = get_json("http://127.0.0.1:18200/status", token, timeout=5)
                code2, diag = get_json("http://127.0.0.1:18200/diagnostics", token, timeout=5)
                good = (staged(code, status, args.node) and code2 == 200 and diag.get("workers") == {}
                        and status.get("preflight", {}).get("checks")
                        and "starting" not in status["preflight"]["checks"])
                streak = streak + 1 if good else 0
                if time.monotonic() - reported >= 10:
                    print("STAGED_WAIT", json.dumps(summary(status)), flush=True)
                    reported = time.monotonic()
            except (OSError, ValueError):
                streak = 0
            if streak >= 3:
                disabled()
                legacy_ready()
                print("GUARDIAN_STAGED_HA_OFF_WORKERS_EMPTY", args.node, json.dumps(summary(status)), flush=True)
                return 0
            require(time.monotonic() < deadline, "Staged agent not confirmed; send output. Maintenance retained")
            time.sleep(2)
    for peer in cfg["nodes"]:
        code, status = get_json(peer["url"].rstrip("/") + "/status", token, timeout=5)
        require(staged(code, status, peer["id"]), "Staged peer not confirmed: " + peer["id"])
        code2, diag = get_json(peer["url"].rstrip("/") + "/diagnostics", token, timeout=5)
        require(code2 == 200 and diag.get("workers") == {}, "Workers not empty: " + peer["id"])
        print("STAGED_PEER", args.node, json.dumps(summary(status)), flush=True)
    conn = pyodbc.connect(os.environ["PERIMETER_HA_SQL"], timeout=5, autocommit=True)
    try:
        conn.timeout = 5
        row = conn.execute("SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
                           "FROM dbo.KPP_HA_Controller WHERE Id=1").fetchone()
        require(row is not None and row[0] == "comparator" and row[1], "Comparator controller lease not confirmed")
    finally:
        conn.close()
    disabled()
    legacy_ready()
    print("STAGED_CLUSTER_LINKS_OK", args.node, flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("STAGED_ROLLOUT_ABORT", str(exc) if isinstance(exc, Abort) else type(exc).__name__, flush=True)
        raise SystemExit(2) from None
