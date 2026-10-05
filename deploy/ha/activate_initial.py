"""Guarded initial cutover of the already installed, passive three-node cluster.

Run check on Comparator, handoff on Perimetr, activate on native Windows, then
report on any node. This utility does not update the runtime or invent RFID data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


RELEASE = "0c573bc708f2906c240af64a23e0f6042006a669"
PRIORITY = [("physical", 1), ("perimetr", 2), ("comparator", 3)]
LEGACY = {
    "RUN_RFID_READER_V3.cmd": "RFID_reader_v4",
    "RUN_RUSGUARD_V3.cmd": "DB_RusGard",
    "RUN_RTSP_V3.cmd": "RTSP",
    "RUN_AGGREGATOR_V3.cmd": "KPP",
    "RUN_WEB_V3.cmd": "web",
}
LAUNCHER = re.compile(r"(?i)\bRUN_(?:RFID_READER|RUSGUARD|RTSP|AGGREGATOR|WEB)_V3\.cmd\b")


class Abort(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Abort(message)


def check_priority(cfg):
    found = sorted([(n["id"], n["priority"]) for n in cfg["nodes"]], key=lambda n: n[1])
    require(found == PRIORITY, "Executor priority must be physical > perimetr > comparator")


def check_agent(node, code, status, prepared=False):
    require(code == 200 and status.get("node") == node, "Agent identity/link failed: " + node)
    require(status.get("sample_age", 999) < 10, "Agent sample is stale: " + node)
    require(status.get("release_sha") == RELEASE, "Unexpected runtime release: " + node)
    require(not status.get("resources", {}).get("restart_required"), "Resource recovery required: " + node)
    if prepared:
        require(status.get("prepared") is True, "Agent is not prepared: " + node)


def legacy_command(name, arguments, root, own_pid=False):
    # The new Guardian uses the OLD venv executable. Inspect script arguments,
    # not argv[0], and exclude this operator process (whose code can name root).
    if own_pid or name.lower() not in ("cmd.exe", "python.exe", "pythonw.exe"):
        return False
    root = str(root).replace("/", "\\").rstrip("\\").casefold()
    return any(root + "\\" in arg.replace("/", "\\").casefold() for arg in arguments[1:])


def same_process(psutil, identity):
    pid, created = identity
    try:
        return psutil.Process(pid).create_time() == created
    except psutil.NoSuchProcess:
        return False


def stop_tree(psutil, process):
    identity = (process.pid, process.create_time())
    command = process.cmdline()
    captured = [(p.pid, p.create_time()) for p in process.children(recursive=True)] + [identity]
    require(same_process(psutil, identity) and psutil.Process(process.pid).cmdline() == command,
            "Legacy launcher changed before stop")
    result = subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    deadline = time.monotonic() + 10
    while any(same_process(psutil, item) for item in captured):
        require(time.monotonic() < deadline, "Captured legacy process remains after taskkill")
        time.sleep(.25)
    # A nonzero taskkill status is accepted ONLY after all captured identities
    # have disappeared; PID reuse is not a reason to kill a different process.
    print("LEGACY_TREE_STOPPED", identity[0], "taskkill_exit=" + str(result.returncode), flush=True)


class Cluster:
    def __init__(self):
        windows = os.name == "nt"
        self.source = Path("D:/PerimeterHA/source" if windows else "/opt/perimeter/source")
        config = Path("D:/PerimeterHA/node.json" if windows else "/etc/perimeter/node.json")
        sys.path[:0] = [str(self.source), str(self.source / "deploy/ha")]
        import pyodbc
        pyodbc.pooling = False
        self.pyodbc = pyodbc
        if windows:
            from windows_tool import read_bundle, windows_environment
            os.environ.update(windows_environment(read_bundle(
                "D:/PerimeterHA/transfer-private/environment.local.json")))
        else:
            from environment_tool import read_generated_environment
            os.environ.update(read_generated_environment("/etc/perimeter/environment"))
        from guardian.sql import SqlStore
        from guardian.probes import get_json, services_health
        self.store, self.get_json, self.services_health = SqlStore(), get_json, services_health
        self.cfg = json.loads(config.read_text(encoding="utf-8-sig"))
        check_priority(self.cfg)
        require(Path(self.cfg["root"]).resolve() == self.source.resolve(), "Unexpected installation root")
        record = json.loads((Path(self.cfg["state_dir"]) / "release.json").read_text(encoding="utf-8-sig"))
        require(record["current"]["sha"] == RELEASE
                and Path(record["current"]["root"]).resolve() == self.source.resolve()
                and not record.get("pending"), "Initial activation requires the prepared runtime")
        self.nodes = {n["id"]: n for n in self.cfg["nodes"]}

    def disabled(self):
        lease = self.store.lease()
        require(lease["enabled"] is False and lease["owner"] is None, "HA is not disabled and unowned")
        return lease

    def controller(self):
        with self.store.connect() as conn:
            row = conn.execute("SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
                               "FROM dbo.KPP_HA_Controller WHERE Id=1").fetchone()
        require(row is not None, "Controller lease is missing")
        return {"owner": row[0], "valid": bool(row[1])}

    def status(self, node, prepared=False):
        code, data = self.get_json(self.nodes[node]["url"].rstrip("/") + "/status",
                                   os.environ["PERIMETER_HA_TOKEN"], timeout=5)
        check_agent(node, code, data, prepared)
        return data

    def passive(self, all_prepared=False):
        rows = {}
        self.disabled()
        for node in self.nodes:
            data = self.status(node, all_prepared or node != "physical")
            require(data.get("active") is False and data.get("faulted") is False,
                    "Agent must be passive and not faulted: " + node)
            rows[node] = data
        self.disabled()
        return rows

    def require_comparator(self):
        require(self.controller() == {"owner": "comparator", "valid": True},
                "Comparator must own the valid controller lease")

    def report(self):
        rows = []
        for node in self.nodes:
            try:
                data = self.status(node)
                row = {k: data.get(k) for k in ("node", "active", "healthy", "prepared", "faulted",
                                               "epoch", "sample_age", "release_sha", "resources")}
                row["services"] = {k: {"ok": v.get("ok"), "status": v.get("detail", {}).get("status"),
                    "dependencies": {key: value.get("status") for key, value in
                                     v.get("detail", {}).get("dependencies", {}).items()}}
                                   for k, v in data.get("services", {}).items()}
            except Exception as exc:
                row = {"node": node, "error": type(exc).__name__}
            rows.append(row)
        report = {"from": self.cfg["node_id"], "priority": PRIORITY,
                  "lease": self.store.lease(), "controller": self.controller(), "nodes": rows}
        print(json.dumps(report, indent=2), flush=True)
        return report

    def handoff(self):
        require(os.name != "nt" and os.geteuid() == 0 and self.cfg["node_id"] == "perimetr",
                "Run handoff through sudo on Perimetr")
        require(self.cfg.get("controller_enabled") is True, "Keep the fallback controller enabled")
        self.passive()
        if self.controller() != {"owner": "comparator", "valid": True}:
            from upgrade_initial_vm import stop_guardian
            try:
                self.disabled()
                stop_guardian()
                deadline = time.monotonic() + 45
                while self.controller() != {"owner": "comparator", "valid": True}:
                    self.disabled()
                    require(time.monotonic() < deadline, "Comparator did not take controller ownership")
                    time.sleep(2)
                self.disabled()
                print("CONTROLLER_ON_COMPARATOR", flush=True)
            finally:
                # Retain the Perimetr agent and fallback controller even on failure.
                subprocess.run(["systemctl", "start", "perimeter-guardian"], check=True, timeout=35)
        deadline = time.monotonic() + 90
        while True:
            self.disabled()
            self.require_comparator()
            try:
                self.passive()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(3)
        self.report()
        print("HANDOFF_OK", flush=True)

    def legacy_processes(self, psutil, root):
        result = []
        for process in psutil.process_iter():
            try:
                if legacy_command(process.name(), process.cmdline(), root, process.pid == os.getpid()):
                    result.append(process)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                # Check inaccessible candidate interpreters conservatively.
                if process.name().lower() in ("cmd.exe", "python.exe", "pythonw.exe"):
                    raise Abort("Cannot inspect a candidate legacy process; use Administrator PowerShell")
        return result

    def launchers(self, psutil, root):
        result = {}
        for process in self.legacy_processes(psutil, root):
            if process.name().lower() != "cmd.exe":
                continue
            match = LAUNCHER.search(" ".join(process.cmdline()[1:]))
            if match:
                name = match.group().upper()
                require(name not in result, "Duplicate legacy launcher: " + name)
                result[name] = process
        return result

    def restore_legacy(self, psutil, root):
        # SQL must freshly confirm HA OFF and every agent must freshly confirm
        # inactivity. Uncertainty is not permission to create unfenced writers.
        self.passive()
        for process in self.launchers(psutil, root).values():
            self.disabled()
            stop_tree(psutil, process)
        require(not self.legacy_processes(psutil, root), "Legacy orphan remains; restart refused")
        self.passive()
        for wrapper, cwd in LEGACY.items():
            subprocess.Popen([os.environ.get("COMSPEC", "cmd.exe"), "/D", "/K", "call",
                              str(root / "deploy" / wrapper)], cwd=str(root / cwd),
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        deadline = time.monotonic() + 90
        while True:
            self.disabled()
            health = self.services_health()
            if len(health) == 5 and all(v.get("ok") is True for v in health.values()):
                print("LEGACY_RESTORED_HA_DISABLED", flush=True)
                return
            require(time.monotonic() < deadline, "Legacy was relaunched but is not fully healthy")
            time.sleep(5)

    def recover_after_failure(self, psutil, root):
        try:
            # A successful fresh OFF/unowned read is also required after an
            # ambiguous commit timeout. Never disable a running HA cluster here.
            self.disabled()
        except Exception:
            print("KEEP_GUARDIANS_RUNNING_DO_NOT_START_LEGACY", flush=True)
            return
        try:
            self.restore_legacy(psutil, root)
        except Exception as recovery:
            print("LEGACY_RECOVERY_FAILED", type(recovery).__name__, flush=True)

    def activate(self):
        require(os.name == "nt" and self.cfg["node_id"] == "physical"
                and self.cfg.get("controller_enabled") is False, "Run activation on physical Windows")
        import ctypes
        import psutil
        require(bool(ctypes.windll.shell32.IsUserAnAdmin()), "Use Administrator PowerShell")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                        "if ((Get-ScheduledTask -TaskName 'PerimeterGuardian').State -ne 'Running') { exit 2 }"],
                       check=True, timeout=20)
        root = Path("D:/Desktop/RFID_KPP-main")
        for wrapper, cwd in LEGACY.items():
            require((root / "deploy" / wrapper).is_file() and (root / cwd).is_dir(),
                    "Legacy rollback files are absent")
        rows = self.passive()
        self.require_comparator()
        checks = rows["physical"].get("preflight", {}).get("checks", {})
        require(len(checks) == 11 and checks.get("ports_free") is False
                and all(value is True for key, value in checks.items() if key != "ports_free"),
                "Physical preflight must fail only on the occupied legacy ports")
        health = self.services_health()
        require(len(health) == 5 and all(v.get("ok") is True for v in health.values()),
                "Legacy services are not all healthy; nothing was stopped")
        # Aggregator can write task results through another connection; do not
        # assume the five-service readiness probe validated that connection.
        identities = []
        for key in ("KPP_CONN_STR", "KPP_TASK_CONN_STR"):
            conn = self.pyodbc.connect(os.environ.get(key) or os.environ["KPP_CONN_STR"],
                                       timeout=5, autocommit=True)
            try:
                conn.timeout = 5
                identities.append(tuple(conn.execute("SELECT CONVERT(nvarchar(128),SERVERPROPERTY('ServerName')),DB_NAME()").fetchone()))
            finally:
                conn.close()
        require(identities[0] == identities[1], "Separate task database requires fencing review")
        targets = self.launchers(psutil, root)
        require(set(targets) == {name.upper() for name in LEGACY}, "Expected exactly five legacy launchers")
        tree_pids = set()
        for process in targets.values():
            tree_pids.add(process.pid)
            tree_pids.update(child.pid for child in process.children(recursive=True))
        require(all(p.pid in tree_pids for p in self.legacy_processes(psutil, root)),
                "An unrelated legacy-root process exists; nothing was stopped")
        print("CUTOVER_PRECHECK_OK", flush=True)
        try:
            for process in targets.values():
                self.disabled()
                stop_tree(psutil, process)
            require(not self.legacy_processes(psutil, root), "Legacy orphan remains; activation refused")
            deadline = time.monotonic() + 120
            while True:
                self.disabled()
                self.require_comparator()
                try:
                    self.passive(all_prepared=True)
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(3)
            require(not self.legacy_processes(psutil, root), "Legacy processes reappeared before activation")
            self.require_comparator()
            self.disabled()
            with self.store.connect() as conn:
                try:
                    changed = conn.execute("UPDATE dbo.KPP_HA_Lease WITH(UPDLOCK,HOLDLOCK) SET Enabled=1 "
                                           "WHERE Id=1 AND Enabled=0 AND Owner IS NULL").rowcount
                    require(changed == 1, "Unexpected lease during activation")
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            require(self.store.lease()["enabled"], "HA enable was not confirmed")
            print("HA_ENABLED", flush=True)
            deadline, streak, identity = time.monotonic() + 240, 0, None
            while True:
                lease = self.store.lease()
                current = (lease["owner"], lease["epoch"])
                try:
                    live = {node: self.status(node) for node in self.nodes}
                    physical = live["physical"]
                    good = (lease["enabled"] and lease["valid"] and lease["owner"] == "physical"
                            and physical.get("active") is True and physical.get("healthy") is True
                            and physical.get("epoch") == lease["epoch"] and physical.get("faulted") is False
                            and len(physical.get("services", {})) == 5
                            and all(v.get("ok") is True for v in physical["services"].values()))
                    good = good and all(live[n].get("active") is False and live[n].get("prepared") is True
                                        and live[n].get("faulted") is False for n in ("perimetr", "comparator"))
                except Exception:
                    good = False
                streak = streak + 1 if good and current == identity else int(good)
                identity = current
                print("HA_WAIT", json.dumps({"owner": lease["owner"], "epoch": lease["epoch"],
                                             "valid": lease["valid"], "healthy_samples": streak}), flush=True)
                if streak >= 3:
                    self.report()
                    print("HA_ACTIVE_PHYSICAL_RESERVES_READY", flush=True)
                    return
                require(time.monotonic() < deadline, "HA is enabled but full physical operation was not confirmed")
                time.sleep(5)
        except Exception:
            self.recover_after_failure(psutil, root)
            try:
                self.report()
            except Exception:
                pass
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "handoff", "activate", "report"))
    args = parser.parse_args(argv)
    cluster = Cluster()
    if args.command == "check":
        require(cluster.cfg["node_id"] == "comparator" and cluster.cfg.get("controller_enabled") is True,
                "Run this precheck on Comparator with its controller enabled")
        cluster.passive()
        cluster.report()
        print("COMPARATOR_PRECHECK_OK", flush=True)
    elif args.command == "handoff":
        cluster.handoff()
    elif args.command == "activate":
        cluster.activate()
    else:
        cluster.report()
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("INITIAL_HA_FAILED", str(exc) if isinstance(exc, Abort) else type(exc).__name__, flush=True)
        raise SystemExit(2)
