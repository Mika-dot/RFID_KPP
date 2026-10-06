"""Comparator Linux: stop its native Guardian, prove Perimetr control, restore.

Run outside the service with sudo. Changes only perimeter-guardian.service on
the local Comparator. No SQL writes, maintenance, source edits or worker kills.
Controller expiry may restart the executor stack; await actual full readiness.
Restored Comparator remains a shadow while Perimetr retains its valid lease.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from urllib.error import URLError
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

RELEASE = "79d1caa3a0709ca96d6ee6d55c8ed73623ca8665"
UNIT = "perimeter-guardian.service"
NODES = ("physical", "perimetr", "comparator")
SERVICES = {"RfidReader", "RusGuardSync", "Yolo", "Aggregator", "WebDashboard"}


class Abort(RuntimeError):
    pass


class Halt(Abort):
    pass


def require(value, message):
    if not value:
        raise Abort(message)


def ready(s, owner, controllers, absent=False):
    lease, rows = s["lease"], s["nodes"]
    leader = rows.get(owner, {})
    expected = {"physical", "perimetr"} if absent else set(NODES)
    return (set(rows) == expected and s["consistent"] and lease["enabled"] is True
            and lease["valid"] is True and lease["owner"] == owner
            and s["controller"]["valid"] is True and s["controller"]["owner"] in controllers
            and leader.get("epoch") == lease["epoch"] and leader.get("active") is True
            and leader.get("healthy") is True and leader.get("prepared") is True
            and leader.get("faulted") is False and leader.get("operator_maintenance") is False
            and set(leader.get("services", {})) == SERVICES
            and all(v.get("ok") is True for v in leader["services"].values())
            and all(r.get("active") is False and r.get("operator_maintenance") is False
                    for n, r in rows.items() if n != owner)
            and (absent or all(r.get("prepared") is True and r.get("faulted") is False
                              for n, r in rows.items() if n != owner)))


def native_identity(properties):
    match = properties.get("ExecStart", "").split("argv[]=", 1)
    argv = shlex.split(match[1].split(";", 1)[0].strip()) if len(match) == 2 else []
    return (properties.get("User") == "perimeter" and properties.get("Restart") == "always"
            and properties.get("KillMode") == "control-group"
            and properties.get("UnitFileState") == "enabled"
            and properties.get("WorkingDirectory") == "/opt/perimeter/source"
            and argv == ["/opt/perimeter/venv/bin/python", "/opt/perimeter/source/guardian/boot.py",
                         "--config", "/etc/perimeter/node.json"])


class Test:
    def __init__(self):
        require(os.name != "nt" and os.geteuid() == 0, "Use sudo on Comparator Linux")
        cfg = json.loads(Path("/etc/perimeter/node.json").read_text(encoding="utf-8-sig"))
        require(cfg["node_id"] == "comparator" and cfg.get("controller_enabled") is True
                and Path(cfg["root"]).resolve() == Path("/opt/perimeter/source")
                and Path(cfg["state_dir"]).resolve() == Path("/var/lib/perimeter")
                and cfg["python"] == "/opt/perimeter/venv/bin/python", "Unexpected Comparator configuration")
        require(sorted((n["priority"], n["id"]) for n in cfg["nodes"])
                == [(1, "physical"), (2, "perimetr"), (3, "comparator")], "Unexpected executor priorities")
        require(UNIT not in Path("/proc/self/cgroup").read_text(), "Run outside the Guardian service")
        self.cfg, self.nodes = cfg, {n["id"]: n for n in cfg["nodes"]}
        state = json.loads((Path(cfg["state_dir"]) / "release.json").read_text())
        require(state["current"]["sha"] == RELEASE and state.get("pending") is False
                and state.get("previous") is None and state.get("fencing_protocol_min") == 2
                and Path(state["current"]["root"]).resolve() == Path(cfg["root"])
                and state["current"].get("python") == cfg["python"], "Pinned stable protocol-2 runtime required")
        r = subprocess.run(["runuser", "-u", "perimeter", "--", "git", "-C", cfg["root"], "rev-parse", "HEAD"],
                           capture_output=True, timeout=15)
        require(r.returncode == 0 and r.stdout.decode().strip() == RELEASE, "Runtime checkout differs from record")
        sys.path[:0] = [cfg["root"], str(Path(cfg["root"]) / "deploy/ha")]
        from environment_tool import read_generated_environment, private_directory, atomic_private_json
        os.environ.update(read_generated_environment("/etc/perimeter/environment"))
        os.environ.update(cfg.get("env", {}))
        from guardian.sql import SqlStore, FENCING_PROTOCOL
        from guardian.probes import get_json
        require(FENCING_PROTOCOL == 2, "Installed SQL code is not protocol 2")
        self.store, self.get_json = SqlStore(), get_json
        self.token = os.environ["PERIMETER_HA_TOKEN"]
        self.private_directory, self.save_json = private_directory, atomic_private_json
        self.journal, self.record = None, None
        require(native_identity(self.native()), "Unexpected Guardian unit identity/settings")

    def native(self):
        fields = ("User", "Restart", "KillMode", "UnitFileState", "WorkingDirectory", "ExecStart",
                  "ActiveState", "SubState", "MainPID")
        r = subprocess.run(["systemctl", "show", UNIT, "--no-pager"] +
                           ["--property=" + f for f in fields], capture_output=True, timeout=15)
        require(r.returncode == 0, "Cannot inspect Guardian unit")
        return dict(line.split("=", 1) for line in r.stdout.decode().splitlines() if "=" in line)

    def service(self, action):
        require(action in ("start", "stop"), "Unexpected service action")
        require(native_identity(self.native()), "Guardian unit identity changed")
        r = subprocess.run(["systemctl", action, UNIT], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        require(r.returncode == 0, "Native Guardian " + action + " failed")

    def status(self, node):
        code, data = self.get_json(self.nodes[node]["url"].rstrip("/") + "/status", self.token, timeout=5)
        require(code == 200 and data.get("node") == node and data.get("release_sha") == RELEASE
                and data.get("fencing_protocol") == 2 and data.get("sample_age", 999) < 10
                and not data.get("resources", {}).get("restart_required", False), "Agent identity/sample failed: " + node)
        return data

    def workers(self, node):
        code, data = self.get_json(self.nodes[node]["url"].rstrip("/") + "/diagnostics", self.token, timeout=5)
        require(code == 200 and isinstance(data.get("workers"), dict), "Worker evidence missing: " + node)
        return data["workers"]

    def observe(self, absent=False):
        before = self.store.lease()
        if before["enabled"] is not True:
            raise Halt("HA disabled externally; test did not change it")
        names = NODES[:2] if absent else NODES
        with ThreadPoolExecutor(max_workers=3) as pool:
            rows = dict(zip(names, pool.map(self.status, names)))
        with self.store.connect() as conn:
            row = conn.execute("SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
                               "FROM dbo.KPP_HA_Controller WHERE Id=1").fetchone()
        require(row is not None, "Controller lease missing")
        after = self.store.lease()
        if after["enabled"] is not True:
            raise Halt("HA disabled externally; test did not change it")
        return {"lease": after, "nodes": rows, "controller": {"owner": row[0], "valid": bool(row[1])},
                "consistent": all(before[k] == after[k] for k in ("owner", "epoch", "valid", "enabled"))}

    def stopped(self):
        p = self.native()
        return p.get("ActiveState") == "inactive" and p.get("MainPID") == "0"

    def offline(self):
        try:
            self.get_json(self.nodes["comparator"]["url"].rstrip("/") + "/status", self.token, timeout=2)
        except (URLError, TimeoutError, ConnectionError):
            return True
        return False

    def wait_ready(self, controllers, absent=False, timeout=600, samples=7):
        deadline, streak, identity = time.monotonic() + timeout, 0, None
        while True:
            try:
                if absent:
                    require(self.stopped() and self.offline(), "Comparator Guardian answered/restarted during failure test")
                s = self.observe(absent)
                lease, rows = s["lease"], s["nodes"]
                owner = lease["owner"] if absent else "physical"
                good = owner in rows and ready(s, owner, controllers, absent)
                if good:
                    worker_rows = {n: self.workers(n) for n in rows}
                    good = all(w == {} for n, w in worker_rows.items() if n != owner)
                    good = good and set(worker_rows[owner]) == SERVICES and all(
                        w.get("running") is True for w in worker_rows[owner].values())
                    final = self.store.lease()
                    good = good and all(lease[k] == final[k] for k in ("owner", "epoch", "valid", "enabled"))
                current = (lease["owner"], lease["epoch"], s["controller"]["owner"])
                streak = streak + 1 if good and identity == current else int(good)
                identity = current
                evidence = {"owner": lease["owner"], "epoch": lease["epoch"], "valid": lease["valid"],
                            "controller": s["controller"], "healthy_samples": streak,
                            "nodes": {n: {k: v.get(k) for k in ("active", "healthy", "prepared", "faulted")}
                                      for n, v in rows.items()}}
            except Halt:
                raise
            except Exception as exc:
                streak, evidence = 0, {"error": type(exc).__name__}
            print("CONTROLLER_TEST_WAIT", json.dumps(evidence), flush=True)
            if streak >= samples:
                return s
            require(time.monotonic() < deadline, "Healthy stack/controller proof timed out")
            time.sleep(10)

    def phase(self, name):
        if self.journal:
            self.record["phase"] = name
            self.record["events"].append({"phase": name, "utc": datetime.now(timezone.utc).isoformat()})
            self.save_json(self.journal, self.record)
        print(name, flush=True)

    def precheck(self):
        p = self.native()
        require(native_identity(p) and p.get("ActiveState") == "active" and p.get("SubState") == "running"
                and int(p.get("MainPID", "0")) > 0, "Comparator Guardian must be running")
        with self.store.connect() as conn:
            triggers = conn.execute("SELECT name,is_disabled,OBJECT_DEFINITION(object_id) FROM sys.triggers "
                                    "WHERE name IN ('HA_RFID_Tags','HA_RusGuardLogs','HA_ReelTransitions',"
                                    "'HA_KPP_ReelEvents','HA_KPP_RuntimeState','HA_KPP_ActiveRfidSessions',"
                                    "'HA_KPP_ProcessingErrors','HA_KPP_EventVideoLinks','HA_KPP_EventSkudLinks')").fetchall()
        require(len(triggers) == 9 and all(not r[1] and r[2] and "Perimeter.HA.Epoch" in r[2]
                and "READCOMMITTEDLOCK" in r[2] and "UPDLOCK" not in r[2] for r in triggers),
                "Nine enabled protocol-2 fencing triggers required")
        self.wait_ready({"comparator"}, timeout=60, samples=3)
        self.phase("CONTROLLER_TEST_PRECHECK_OK")

    def stop_passive(self):
        s = self.observe()
        require(ready(s, "physical", {"comparator"}) and self.workers("comparator") == {},
                "Cluster changed before stopping passive Comparator")
        final = self.store.lease()
        require(all(final[k] == s["lease"][k] for k in ("owner", "epoch", "valid", "enabled")),
                "Executor lease changed before native stop")
        self.service("stop")
        require(self.stopped(), "Comparator native stop unconfirmed")
        self.phase("COMPARATOR_GUARDIAN_STOPPED")

    def recover(self):
        self.service("start")
        self.phase("COMPARATOR_GUARDIAN_START_REQUESTED")
        s = self.wait_ready({"perimetr", "comparator"})
        self.phase("THREE_NODES_READY_PHYSICAL_ACTIVE")
        print("RESTORED_CONTROLLER", s["controller"]["owner"], flush=True)
        return s

    def run(self):
        self.precheck()
        folder = Path(self.cfg["state_dir"]) / "acceptance"
        self.private_directory(folder)
        self.journal = folder / ("controller-" + uuid.uuid4().hex + ".json")
        self.record = {"release": RELEASE, "node": "comparator", "changed": True,
                       "fallback_proven": False, "events": []}
        changed = False
        try:
            self.phase("COMPARATOR_STOP_INTENT")
            changed = True
            print("CONTROLLER_TEST_JOURNAL", str(self.journal), flush=True)
            self.stop_passive()
            self.wait_ready({"perimetr"}, absent=True, timeout=300)
            self.record["fallback_proven"] = True
            self.phase("PERIMETR_CONTROLLER_RENEWAL_AND_FULL_STACK_PROVEN")
            self.recover()
            self.phase("CONTROLLER_FALLBACK_TEST_OK")
        except BaseException:
            if changed:
                try:
                    self.recover()
                except BaseException as exc:
                    print("CONTROLLER_RECOVERY_UNCONFIRMED", str(exc) if isinstance(exc, Abort) else type(exc).__name__, flush=True)
                    print("START_COMPARATOR_GUARDIAN_AND_USE_RECOVER; DO_NOT_START_LEGACY_OR_DISABLE_HA", flush=True)
            raise


def main(argv=None):
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--controller-test", action="store_true")
    modes.add_argument("--recover", type=Path, metavar="JOURNAL")
    args = parser.parse_args(argv)
    app = Test()
    from common.single_instance import SingleInstanceLock
    with SingleInstanceLock(str(Path(app.cfg["state_dir"]) / "operator-cutover.lock")):
        if args.recover:
            root = (Path(app.cfg["state_dir"]) / "acceptance").resolve()
            journal = args.recover.resolve()
            require(journal.is_relative_to(root) and journal.name.startswith("controller-") and journal.suffix == ".json",
                    "Unexpected recovery journal path")
            app.journal, app.record = journal, json.loads(journal.read_text())
            require(app.record.get("release") == RELEASE and app.record.get("node") == "comparator"
                    and app.record.get("changed") is True and isinstance(app.record.get("events"), list),
                    "An interrupted Comparator controller test journal is required")
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
        print("CONTROLLER_TEST_STOPPED", str(exc) if isinstance(exc, Abort) else type(exc).__name__, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
