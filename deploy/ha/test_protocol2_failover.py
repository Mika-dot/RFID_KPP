"""Native physical Windows: real Aggregator failure, Perimetr takeover, failback.

No SQL writes, lease override, source update, legacy startup or business reset.
Kill only a registered, identified HA Aggregator; use guarded maintenance API
after demotion. The installed controller alone elects and fences executors.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

RELEASE = "79d1caa3a0709ca96d6ee6d55c8ed73623ca8665"
NODES = ("physical", "perimetr", "comparator")
SERVICES = {"RfidReader", "RusGuardSync", "Yolo", "Aggregator", "WebDashboard"}


class Abort(RuntimeError):
    pass


class Halt(Abort):
    """An external HA mode change must not be hidden as transient telemetry."""


def require(ok, message):
    if not ok:
        raise Abort(message)


def normalized(value):
    return str(value).replace("\\", "/").rstrip("/").casefold()


def aggregator_identity(argv, executable, env, cfg, lease):
    root = normalized(cfg["root"])
    return (lease.get("enabled") is True and lease.get("valid") is True
            and lease.get("owner") == "physical" and len(argv) == 7 and argv[1] == "-u"
            and normalized(executable) == normalized(cfg["python"])
            and normalized(argv[2]) == root + "/deploy/run_service.py"
            and argv[3:5] == ["--service", "Perimeter.Aggregator"] and argv[5] == "--script"
            and normalized(argv[6]) == root + "/deploy/monitored_aggregator.py"
            and env.get("PERIMETER_HA_NODE") == "physical"
            and env.get("PERIMETER_HA_EPOCH") == str(lease.get("epoch"))
            and normalized(env.get("PERIMETER_HA_STATE_DIR", "")) == normalized(cfg["state_dir"]))


def ready(snapshot, owner, held=False, controller="comparator", held_nodes=()):
    lease, rows = snapshot["lease"], snapshot["nodes"]
    leader = rows[owner]
    holds = set(held_nodes) | ({"physical"} if held else set())
    return (snapshot["consistent"] and lease["enabled"] is True and lease["valid"] is True
            and lease["owner"] == owner and leader.get("epoch") == lease["epoch"]
            and snapshot["controller"] == {"owner": controller, "valid": True}
            and leader.get("active") is True and leader.get("healthy") is True
            and leader.get("prepared") is True
            and leader.get("faulted") is False and leader.get("operator_maintenance") is False
            and set(leader.get("services", {})) == SERVICES
            and all(h.get("ok") is True for h in leader["services"].values())
            and all(rows[n].get("active") is False for n in NODES if n != owner)
            and all(rows[n].get("prepared") is True and rows[n].get("faulted") is False
                    and rows[n].get("operator_maintenance") is False
                    for n in NODES if n != owner and n not in holds)
            and all(rows[n].get("operator_maintenance") is True for n in holds))


class Test:
    def __init__(self):
        require(os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin()),
                "Use Administrator PowerShell on physical Windows")
        self.cfg = json.loads(Path("D:/PerimeterHA/node.json").read_text(encoding="utf-8-sig"))
        self.root = Path(self.cfg["root"])
        require(self.cfg["node_id"] == "physical" and self.cfg.get("controller_enabled") is False
                and self.root.resolve() == Path("D:/PerimeterHA/source").resolve(), "Unexpected physical configuration")
        require(sorted(((n["id"], n["priority"]) for n in self.cfg["nodes"]), key=lambda x: x[1])
                == [("physical", 1), ("perimetr", 2), ("comparator", 3)], "Unexpected executor priorities")
        sys.path[:0] = [str(self.root), str(self.root / "deploy/ha")]
        from windows_tool import read_bundle, windows_environment
        os.environ.update(windows_environment(read_bundle("D:/PerimeterHA/transfer-private/environment.local.json")))
        from guardian.sql import SqlStore
        from guardian.probes import get_json
        from environment_tool import private_directory, atomic_private_json
        self.store, self.get_json = SqlStore(), get_json
        self.private_directory, self.save_json = private_directory, atomic_private_json
        self.token, self.nodes = os.environ["PERIMETER_HA_TOKEN"], {n["id"]: n for n in self.cfg["nodes"]}
        record = json.loads((Path(self.cfg["state_dir"]) / "release.json").read_text(encoding="utf-8-sig"))
        require(record["current"]["sha"] == RELEASE and record.get("fencing_protocol_min") == 2
                and record.get("pending") is False and record.get("previous") is None
                and Path(record["current"]["root"]).resolve() == self.root.resolve(), "Pinned stable protocol-2 runtime required")
        result = subprocess.run(["C:/Program Files/Git/cmd/git.exe", "-C", str(self.root), "rev-parse", "HEAD"],
                                capture_output=True, timeout=15)
        require(result.returncode == 0 and result.stdout.decode().strip() == RELEASE, "Runtime checkout differs from record")
        self.journal, self.record = None, None

    def status(self, node):
        code, data = self.get_json(self.nodes[node]["url"].rstrip("/") + "/status", self.token, timeout=5)
        require(code == 200 and data.get("node") == node and data.get("release_sha") == RELEASE
                and data.get("fencing_protocol") == 2 and data.get("sample_age", 999) < 10
                and not data.get("resources", {}).get("restart_required", False), "Agent identity/sample failed: " + node)
        return data

    def workers(self, node):
        code, data = self.get_json(self.nodes[node]["url"].rstrip("/") + "/diagnostics", self.token, timeout=5)
        require(code == 200 and isinstance(data.get("workers"), dict), "Worker evidence missing: " + node)
        return data["workers"]  # Never print endpoint log tails or full status details.

    def observe(self):
        before = self.store.lease()
        if before["enabled"] is not True:
            raise Halt("HA was disabled; test cannot continue")
        with ThreadPoolExecutor(max_workers=3) as pool:
            rows = dict(zip(NODES, pool.map(self.status, NODES)))
        with self.store.connect() as conn:
            row = conn.execute("SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
                               "FROM dbo.KPP_HA_Controller WHERE Id=1").fetchone()
        require(row is not None, "Controller lease missing")
        after = self.store.lease()
        if after["enabled"] is not True:
            raise Halt("HA was disabled; test cannot continue")
        consistent = all(before[k] == after[k] for k in ("owner", "epoch", "valid", "enabled"))
        return {"lease": after, "nodes": rows, "consistent": consistent,
                "controller": {"owner": row[0], "valid": bool(row[1])}}

    def maintain(self, enabled):
        code, data = self.get_json(self.nodes["physical"]["url"].rstrip("/") + "/maintenance", self.token,
                                   timeout=15, body={"enabled": enabled, "release": RELEASE})
        require(code == 200 and data.get("operator_maintenance") is enabled and data.get("stopped") is True,
                "Physical maintenance transition not confirmed")

    def phase(self, name):
        if self.journal:
            self.record["phase"] = name
            self.record["events"].append({"phase": name, "utc": datetime.now(timezone.utc).isoformat()})
            self.save_json(self.journal, self.record)
        print(name, flush=True)

    def ready_snapshot(self, snapshot, owner, held):
        return ready(snapshot, owner, held)

    def wait_ready(self, owner, held=False, timeout=600, samples=7):
        deadline, streak, identity = time.monotonic() + timeout, 0, None
        while True:
            evidence = {}
            try:
                snapshot = self.observe()
                lease, rows = snapshot["lease"], snapshot["nodes"]
                good = self.ready_snapshot(snapshot, owner, held)
                if good:
                    good = all(self.workers(n) == {} for n in NODES if n != owner)
                    leader_workers = self.workers(owner)
                    good = good and set(leader_workers) == SERVICES and all(v.get("running") is True for v in leader_workers.values())
                    final = self.store.lease()
                    good = good and all(lease[k] == final[k] for k in ("owner", "epoch", "valid", "enabled"))
                current = (lease["owner"], lease["epoch"], snapshot["controller"]["owner"])
                streak = streak + 1 if good and identity == current else int(good)
                identity = current
                evidence = {"owner": lease["owner"], "epoch": lease["epoch"], "valid": lease["valid"],
                    "healthy_samples": streak, "controller": snapshot["controller"], "nodes": {
                    n: {**{k: s.get(k) for k in ("active", "healthy", "prepared", "faulted", "operator_maintenance")},
                        "services": {k: v.get("ok") for k, v in s.get("services", {}).items()}}
                    for n, s in rows.items()}}
            except Halt:
                raise
            except Exception as exc:
                streak, evidence = 0, {"error": type(exc).__name__}
            print("FAILOVER_TEST_WAIT", owner, json.dumps(evidence), flush=True)
            if streak >= samples:
                return snapshot
            require(time.monotonic() < deadline, "Stable full stack not confirmed on " + owner)
            time.sleep(10)

    def precheck(self):
        task_check = ("$t=Get-ScheduledTask -TaskName 'PerimeterGuardian'; "
                      "$boot=@($t.Triggers | Where-Object {$_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' -and $_.Enabled -ne $false}); "
                      "if($t.State -ne 'Running' -or -not $t.Settings.Enabled -or $boot.Count -ne 1 "
                      "-or $t.Settings.ExecutionTimeLimit -ne 'PT0S' "
                      "-or $t.Principal.UserId -notin @('SYSTEM','СИСТЕМА','S-1-5-18')){exit 2}")
        native = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", task_check],
                                capture_output=True, timeout=20)
        require(native.returncode == 0, "Physical boot task is not running/enabled/unlimited/SYSTEM")
        with self.store.connect() as conn:
            triggers = conn.execute("SELECT name,is_disabled,OBJECT_DEFINITION(object_id) FROM sys.triggers "
                                    "WHERE name IN ('HA_RFID_Tags','HA_RusGuardLogs','HA_ReelTransitions',"
                                    "'HA_KPP_ReelEvents','HA_KPP_RuntimeState','HA_KPP_ActiveRfidSessions',"
                                    "'HA_KPP_ProcessingErrors','HA_KPP_EventVideoLinks','HA_KPP_EventSkudLinks')").fetchall()
        require(len(triggers) == 9 and all(not row[1] and row[2] and "Perimeter.HA.Epoch" in row[2]
                and "READCOMMITTEDLOCK" in row[2] and "UPDLOCK" not in row[2] for row in triggers),
                "Nine enabled protocol-2 triggers required")
        self.wait_ready("physical", timeout=60, samples=3)
        self.phase("FAILOVER_TEST_PRECHECK_OK")

    def capture_aggregator(self):
        import psutil
        lease = self.store.lease()
        entries = json.loads((Path(self.cfg["state_dir"]) / "children.json").read_text(encoding="utf-8-sig"))
        require(len(entries) == 5 and len({row["pid"] for row in entries}) == 5, "Five registered physical HA workers required")
        targets = []
        for row in entries:
            process = psutil.Process(row["pid"])
            require(abs(process.create_time() - row["created"]) <= .01, "Registered worker PID identity changed")
            argv = process.cmdline()
            if aggregator_identity(argv, process.exe(), process.environ(), self.cfg, lease):
                targets.append((process, argv, row["created"], lease["epoch"]))
        require(len(targets) == 1, "Exactly one registered physical HA Aggregator required")
        return targets[0]

    def stop_aggregator(self, target):
        import psutil
        process, argv, created, epoch = target
        lease = self.store.lease()
        require(lease["epoch"] == epoch and process.create_time() == created and process.cmdline() == argv
                and aggregator_identity(argv, process.exe(), process.environ(), self.cfg, lease),
                "Aggregator or executor identity changed before failure injection")
        identities = [(p.pid, p.create_time()) for p in process.children(recursive=True)] + [(process.pid, created)]
        result = subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        def survives(item):
            try:
                return psutil.Process(item[0]).create_time() == item[1]
            except psutil.NoSuchProcess:
                return False
        deadline = time.monotonic() + 10
        while any(survives(i) for i in identities):
            require(time.monotonic() < deadline, "Captured Aggregator tree remains after taskkill")
            time.sleep(.25)
        print("HA_AGGREGATOR_STOPPED", process.pid, "taskkill_exit=" + str(result.returncode), flush=True)

    def hold_demoted_physical(self):
        deadline = time.monotonic() + 120
        reserve = getattr(self, "reserve", "perimetr")
        while True:
            try:
                snapshot = self.observe()
                lease, physical = snapshot["lease"], snapshot["nodes"]["physical"]
                if snapshot["consistent"] and lease["owner"] == reserve and lease["valid"]:
                    if physical.get("active") is False and self.workers("physical") == {}:
                        self.phase("PHYSICAL_MAINTENANCE_REQUESTED")
                        self.maintain(True)
                        self.phase("PHYSICAL_HELD_FOR_RESERVE_TEST")
                        return
                print("AUTOMATIC_DEMOTION_WAIT", json.dumps({"owner": lease["owner"], "epoch": lease["epoch"],
                      "valid": lease["valid"], "physical_active": physical.get("active")}), flush=True)
            except Halt:
                raise
            except Exception as exc:
                print("AUTOMATIC_DEMOTION_WAIT", type(exc).__name__, flush=True)
            require(time.monotonic() < deadline, "Automatic reserve takeover and physical demotion not confirmed")
            time.sleep(2)

    def release_physical(self):
        # Do not reset verification repeatedly after a successful response.
        deadline = time.monotonic() + 120
        while True:
            try:
                lease = self.store.lease()
                if lease["enabled"] is not True:
                    raise Halt("HA disabled externally; recovery did not change it")
                status = self.status("physical")
                if status.get("operator_maintenance") is False and status.get("faulted") is False:
                    self.phase("PHYSICAL_AVAILABLE_FOR_AUTOMATIC_FAILBACK")
                    return
                if not (lease["owner"] == "physical" and lease["valid"]):
                    if status.get("operator_maintenance") is not True:
                        self.maintain(True)  # Establish independent verification if failure preceded the hold.
                    self.maintain(False)
                    self.phase("PHYSICAL_RELEASED_FOR_INDEPENDENT_RECOVERY")
                    return
            except Halt:
                raise
            except Exception as exc:
                print("PHYSICAL_RELEASE_WAIT", type(exc).__name__, flush=True)
            require(time.monotonic() < deadline, "Physical maintenance release unconfirmed; use --recover with journal")
            time.sleep(2)

    def recover(self):
        self.release_physical()
        self.wait_ready("physical")
        self.phase("PHYSICAL_FULL_STACK_RESTORED_HA_ON")

    def run(self):
        self.precheck()
        target = self.capture_aggregator()
        folder = Path(self.cfg["state_dir"]) / "acceptance"
        self.private_directory(folder)
        self.journal = folder / ("failover-" + uuid.uuid4().hex + ".json")
        self.record = {"release": RELEASE, "changed": False, "reserve_proven": False, "events": [],
                       "aggregator_pid": target[0].pid, "created": target[2], "initial_epoch": target[3]}
        self.phase("FAILOVER_TEST_CAPTURED")
        print("FAILOVER_TEST_JOURNAL", str(self.journal), flush=True)
        changed = False
        try:
            self.record["changed"], changed = True, True
            self.phase("AGGREGATOR_FAILURE_REQUESTED")
            self.stop_aggregator(target)
            self.hold_demoted_physical()
            self.wait_ready("perimetr", held=True, timeout=300)
            self.record["reserve_proven"] = True
            self.phase("PERIMETR_FULL_STACK_PROVEN")
            self.recover()
            self.phase("FAILOVER_AND_FAILBACK_PROTOCOL2_OK")
        except BaseException:
            if changed:
                try:
                    self.recover()
                except BaseException as exc:
                    print("FAILOVER_TEST_RECOVERY_UNCONFIRMED", str(exc) if isinstance(exc, Abort) else type(exc).__name__, flush=True)
                    print("DO_NOT_START_LEGACY_OR_DISABLE_HA; USE_RECOVER_WITH_JOURNAL", flush=True)
            raise


def main(argv=None):
    sys.dont_write_bytecode = True
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--failover-test", action="store_true")
    modes.add_argument("--recover", type=Path, metavar="JOURNAL")
    args = parser.parse_args(argv)
    app = Test()
    from common.single_instance import SingleInstanceLock
    with SingleInstanceLock(str(Path(app.cfg["state_dir"]) / "operator-cutover.lock")):
        if args.recover:
            journal = args.recover.resolve()
            root = (Path(app.cfg["state_dir"]) / "acceptance").resolve()
            require(journal.is_relative_to(root) and journal.name.startswith("failover-") and journal.suffix == ".json",
                    "Unexpected recovery journal path")
            app.journal, app.record = journal, json.loads(journal.read_text(encoding="utf-8"))
            require(app.record.get("release") == RELEASE and app.record.get("changed") is True,
                    "An interrupted test journal is required")
            app.recover()
        elif args.check:
            app.precheck()
        else:
            app.run()
    return 0


def cli():
    try:
        return main()
    except (Exception, KeyboardInterrupt) as exc:
        print("FAILOVER_TEST_STOPPED", str(exc) if isinstance(exc, Abort) else type(exc).__name__, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
