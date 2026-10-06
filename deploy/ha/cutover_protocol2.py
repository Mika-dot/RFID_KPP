"""Guarded legacy -> protocol-2 HA cutover of the pinned, staged three nodes.

Native Administrator Windows only. No source/config/spool/cursor/latch resets.
Back up exact legacy launcher commands locally; never print secrets or argv.
On failure restore legacy only after SQL OFF/unowned and all HA workers stopped.
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
from pathlib import Path

RELEASE = "79d1caa3a0709ca96d6ee6d55c8ed73623ca8665"
PRIORITY = [("physical", 1), ("perimetr", 2), ("comparator", 3)]
CHECKS = {"space", "sql_fencing", "same_output_database", "entrypoints", "configuration",
          "model", "masks", "dll_file", "ports_free", "sdk_load", "python_dependencies"}


class Abort(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Abort(message)


def agent_identity(code, s, node):
    return (code == 200 and s.get("node") == node and s.get("release_sha") == RELEASE
            and s.get("fencing_protocol") == 2 and s.get("sample_age", 999) < 10
            and not s.get("resources", {}).get("restart_required", False))


def preflight_ok(s, legacy=False):
    checks = s.get("preflight", {}).get("checks", {})
    return (set(checks) == CHECKS and all(v is True for k, v in checks.items() if k != "ports_free")
            and checks["ports_free"] is (not legacy))


def trigger_protocol2(definition):
    return bool(definition and "Perimeter.HA.Epoch" in definition and
                "READCOMMITTEDLOCK" in definition and "UPDLOCK" not in definition)


def cluster_healthy(lease, rows, controller):
    physical = rows.get("physical", {})
    return (lease.get("enabled") is True and lease.get("valid") is True
            and lease.get("owner") == "physical" and physical.get("active") is True
            and physical.get("healthy") is True and physical.get("epoch") == lease.get("epoch")
            and controller == {"owner": "comparator", "valid": True}
            and all(rows.get(n, {}).get("faulted") is False for n, _ in PRIORITY)
            and len(physical.get("services", {})) == 5
            and all(v.get("ok") is True for v in physical["services"].values())
            and all(rows.get(n, {}).get("active") is False and rows.get(n, {}).get("prepared") is True
                    for n in ("perimetr", "comparator")))


class Cutover:
    def __init__(self):
        require(os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin()),
                "Use Administrator PowerShell on physical Windows")
        self.source = Path("D:/PerimeterHA/source").resolve()
        self.legacy = Path("D:/Desktop/RFID_KPP-main").resolve()
        self.config = Path("D:/PerimeterHA/node.json")
        self.cfg = json.loads(self.config.read_text(encoding="utf-8-sig"))
        require(self.cfg["node_id"] == "physical" and self.cfg.get("controller_enabled") is False
                and Path(self.cfg["root"]).resolve() == self.source, "Unexpected physical configuration")
        require(sorted(((n["id"], n["priority"]) for n in self.cfg["nodes"]), key=lambda n: n[1]) == PRIORITY,
                "Unexpected executor priorities")
        sys.path[:0] = [str(self.source), str(self.source / "deploy/ha")]
        from windows_tool import read_bundle, windows_environment
        os.environ.update(windows_environment(read_bundle(
            "D:/PerimeterHA/transfer-private/environment.local.json")))
        from guardian.sql import SqlStore, control_odbc, epoch_barrier, FENCING_PROTOCOL
        from guardian.probes import get_json, services_health
        from environment_tool import private_directory, atomic_private_json
        from activate_initial import LEGACY, LAUNCHER, legacy_command, same_process, stop_tree
        from repair_activation_runtime import OUTPUT_TABLES, migration_connection, check_database_barrier
        self.store, self.driver, self.barrier = SqlStore(), control_odbc(), epoch_barrier
        self.get_json, self.services_health = get_json, services_health
        self.private_directory, self.save_json = private_directory, atomic_private_json
        self.wrappers, self.launcher_pattern = LEGACY, LAUNCHER
        self.legacy_command, self.same_process, self.stop_tree = legacy_command, same_process, stop_tree
        self.tables, self.migration_login, self.barrier_check = OUTPUT_TABLES, migration_connection, check_database_barrier
        require(FENCING_PROTOCOL == 2, "Installed runtime is not protocol 2")
        self.token = os.environ["PERIMETER_HA_TOKEN"]
        self.nodes = {n["id"]: n for n in self.cfg["nodes"]}
        record = json.loads((Path(self.cfg["state_dir"]) / "release.json").read_text(encoding="utf-8-sig"))
        require(record["current"]["sha"] == RELEASE and record.get("fencing_protocol_min") == 2
                and record.get("pending") is False and record.get("previous") is None
                and Path(record["current"]["root"]).resolve() == self.source, "Pinned stopped release required")
        r = subprocess.run(["C:/Program Files/Git/cmd/git.exe", "-C", str(self.source), "rev-parse", "HEAD"],
                           capture_output=True, timeout=15)
        require(r.returncode == 0 and r.stdout.decode().strip() == RELEASE, "Checkout differs from record")
        self.operator_token, self.old_controller = str(uuid.uuid4()), None
        self.backup, self.saved = None, []

    def disabled(self):
        lease = self.store.lease()
        require(lease["enabled"] is False and lease["owner"] is None, "HA must be OFF and unowned")
        return lease

    def controller(self):
        with self.store.connect() as conn:
            row = conn.execute("SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
                               "FROM dbo.KPP_HA_Controller WHERE Id=1").fetchone()
        require(row is not None, "Controller lease missing")
        return {"owner": row[0], "valid": bool(row[1])}

    def status(self, node):
        code, s = self.get_json(self.nodes[node]["url"].rstrip("/") + "/status", self.token, timeout=5)
        require(agent_identity(code, s, node), "Agent identity/link/resource check failed: " + node)
        return s

    def empty_workers(self, node):
        code, d = self.get_json(self.nodes[node]["url"].rstrip("/") + "/diagnostics", self.token, timeout=5)
        require(code == 200 and d.get("workers") == {}, "HA workers remain: " + node)

    def passive(self, maintenance=None, ready=False):
        self.disabled()
        rows = {}
        for node in self.nodes:
            s = self.status(node)
            require(s.get("active") is False, "An HA executor is active: " + node)
            if maintenance is not None:
                require(s.get("operator_maintenance") is maintenance, "Unexpected maintenance state: " + node)
            if ready:
                require(s.get("prepared") is True and s.get("faulted") is False and preflight_ok(s),
                        "Independent recovery not yet verified: " + node)
            self.empty_workers(node)
            rows[node] = s
        self.disabled()
        return rows

    def maintenance(self, enabled):
        for node, _ in PRIORITY:
            code, d = self.get_json(self.nodes[node]["url"].rstrip("/") + "/maintenance", self.token,
                                   timeout=10, body={"enabled": enabled, "release": RELEASE})
            require(code == 200 and d.get("operator_maintenance") is enabled and d.get("stopped") is True,
                    "Maintenance transition not confirmed: " + node)

    def phase(self, name):
        if self.backup:
            self.save_json(self.backup / "phase.json", {"phase": name, "release": RELEASE})
        print(name, flush=True)

    def connect(self, login=None):
        conn = self.driver.connect(login or os.environ["PERIMETER_HA_SQL"], timeout=5, autocommit=False)
        conn.timeout = 20
        conn.execute("SET XACT_ABORT ON; SET LOCK_TIMEOUT 5000;")
        return conn

    def hold_controller(self):
        # HA OFF makes this a deployment interlock, not an executor election.
        # Suppress deterministic/LLM repair while nodes independently verify.
        conn = self.connect()
        try:
            self.barrier(conn, timeout=5000)
            # Protocol-1 legacy triggers may retain an update lock for a long
            # transaction. A short shared read is compatible; take the lease
            # update lock only AFTER the captured legacy processes are stopped.
            lease = conn.execute("SELECT Enabled,Owner FROM dbo.KPP_HA_Lease WITH(READCOMMITTEDLOCK) WHERE Id=1").fetchone()
            require(lease is not None and not lease[0] and lease[1] is None, "HA changed before deployment interlock")
            row = conn.execute("SELECT Owner,Token,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
                               "FROM dbo.KPP_HA_Controller WITH(UPDLOCK,HOLDLOCK) WHERE Id=1").fetchone()
            require(row is not None and row[0] == "comparator" and row[2], "Comparator controller required")
            self.old_controller = {"owner": row[0], "token": row[1]}
            self.save_json(self.backup / "controller.json", {"original": self.old_controller,
                           "operator_token": self.operator_token})
            conn.execute("UPDATE dbo.KPP_HA_Controller SET Owner=N'operator-cutover',Token=?,"
                         "ExpiresAt=DATEADD(second,180,SYSUTCDATETIME()) WHERE Id=1", self.operator_token)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.phase("DEPLOYMENT_CONTROLLER_INTERLOCK")

    def renew_interlock(self):
        self.disabled()
        conn = self.connect()
        try:
            count = conn.execute("UPDATE dbo.KPP_HA_Controller SET ExpiresAt=DATEADD(second,180,SYSUTCDATETIME()) "
                                 "WHERE Id=1 AND Owner=N'operator-cutover' AND Token=? "
                                 "AND ExpiresAt>SYSUTCDATETIME()", self.operator_token).rowcount
            require(count == 1, "Deployment controller interlock lost")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_interlock(self, verify=False):
        if self.old_controller is None:
            return
        self.disabled()
        conn = self.connect()
        try:
            restored = conn.execute("UPDATE dbo.KPP_HA_Controller SET Owner=?,Token=?,"
                                    "ExpiresAt=DATEADD(second,15,SYSUTCDATETIME()) "
                                    "OUTPUT inserted.ExpiresAt WHERE Id=1 AND Token=?",
                                    self.old_controller["owner"], self.old_controller["token"],
                                    self.operator_token).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        if not verify:
            return
        require(restored is not None, "Deployment interlock changed before handoff")
        deadline = time.monotonic() + 10
        while True:
            self.disabled()
            with self.store.connect() as conn:
                row = conn.execute("SELECT Owner,Token,ExpiresAt FROM dbo.KPP_HA_Controller WHERE Id=1").fetchone()
            if row is not None and row[0] == "comparator" and row[1] == self.old_controller["token"] and row[2] > restored[0]:
                self.phase("COMPARATOR_CONTROLLER_RENEWAL_CONFIRMED")
                return
            require(time.monotonic() < deadline, "Comparator did not renew restored controller identity")
            time.sleep(1)

    def legacy_processes(self):
        import psutil
        result = []
        for p in psutil.process_iter():
            try:
                if p.pid == os.getpid() or p.name().lower() not in ("cmd.exe", "python.exe", "pythonw.exe"):
                    continue
                if self.legacy_command(p.name(), p.cmdline(), self.legacy):
                    result.append(p)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                raise Abort("Cannot inspect a candidate legacy process") from None
        return result

    def capture_legacy(self):
        for wrapper, cwd in self.wrappers.items():
            require((self.legacy / "deploy" / wrapper).is_file() and (self.legacy / cwd).is_dir(),
                    "Legacy rollback files missing")
        targets, covered = {}, set()
        processes = self.legacy_processes()
        for p in processes:
            if p.name().lower() == "cmd.exe":
                match = self.launcher_pattern.search(" ".join(p.cmdline()[1:]))
                if match:
                    name = match.group().upper()
                    require(name not in targets, "Duplicate legacy launcher")
                    targets[name] = p
        require(set(targets) == set(self.wrappers), "Exactly five known legacy launchers required")
        for name, p in targets.items():
            covered.update([p.pid] + [c.pid for c in p.children(recursive=True)])
            self.saved.append({"wrapper": name, "pid": p.pid, "created": p.create_time(),
                               "argv": p.cmdline(), "cwd": p.cwd()})
        require(all(p.pid in covered for p in processes), "Unrelated legacy-root process found")
        self.save_json(self.backup / "legacy-launchers.json", self.saved)
        return targets

    def migrate(self, login):
        self.renew_interlock()
        self.passive(maintenance=True)
        require(not self.legacy_processes(), "Legacy processes remain before migration")
        conn = self.connect(login)
        try:
            self.barrier(conn, timeout=5000)
            row = conn.execute("SELECT Enabled,Owner FROM dbo.KPP_HA_Lease WITH(UPDLOCK,HOLDLOCK) WHERE Id=1").fetchone()
            require(row is not None and not row[0] and row[1] is None, "HA changed before migration")
            originals = {}
            for table in self.tables:
                row = conn.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?)),is_disabled FROM sys.triggers "
                                   "WHERE object_id=OBJECT_ID(?)", "dbo.HA_" + table, "dbo.HA_" + table).fetchone()
                require(row is not None and row[0] and not row[1], "Enabled fencing trigger missing: " + table)
                originals[table] = row[0]
            self.save_json(self.backup / "triggers.json", originals)
            cursor = conn.execute((self.source / "migrations/004_perimeter_ha_epoch_barrier.sql").read_text(encoding="utf-8-sig"))
            while cursor.nextset():
                pass
            for table in self.tables:
                d = conn.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?))", "dbo.HA_" + table).fetchone()[0]
                require(trigger_protocol2(d), "Protocol-2 trigger not verified: " + table)
            conn.commit()
        except BaseException:
            conn.rollback()
            self.phase("SQL_MIGRATION_ROLLED_BACK")
            raise
        finally:
            conn.close()
        self.phase("SQL_PROTOCOL2_INSTALLED_HA_OFF")
        self.barrier_check(self.store)

    def fence_off(self):
        # Disabling bypasses immutable worker context only AFTER all existing
        # writer transactions finish behind the exclusive epoch barrier.
        lease = self.store.lease()
        if lease["enabled"] is False and lease["owner"] is None:
            return
        conn = self.connect()
        try:
            self.barrier(conn, timeout=5000)
            row = conn.execute("SELECT Enabled,Owner FROM dbo.KPP_HA_Lease WITH(UPDLOCK,HOLDLOCK) WHERE Id=1").fetchone()
            require(row is not None, "Executor lease missing during rollback")
            if row[0] or row[1] is not None:
                conn.execute("UPDATE dbo.KPP_HA_Lease SET Enabled=0,Owner=NULL,Epoch=Epoch+1,"
                             "ExpiresAt=SYSUTCDATETIME() WHERE Id=1")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.disabled()

    def rollback(self):
        import psutil
        self.fence_off()
        deadline = time.monotonic() + 90
        while True:
            try:
                self.maintenance(True)
                self.passive(maintenance=True)
                break
            except Exception:
                require(time.monotonic() < deadline, "Rollback cannot confirm all agents stopped; legacy restart refused")
                time.sleep(2)
        # Stop only captured surviving launcher identities, never a reused PID.
        for row in self.saved:
            if self.same_process(psutil, (row["pid"], row["created"])):
                p = psutil.Process(row["pid"])
                require(p.cmdline() == row["argv"], "Captured legacy identity changed")
                self.stop_tree(psutil, p)
        require(not self.legacy_processes(), "Legacy orphan/unknown process remains; restart refused")
        require(len(self.saved) == 5, "Durable five-launcher rollback capture missing")
        self.disabled()
        self.release_interlock()
        self.passive(maintenance=True)
        for row in self.saved:
            self.disabled()
            subprocess.Popen(row["argv"], cwd=row["cwd"], creationflags=subprocess.CREATE_NEW_CONSOLE)
        deadline = time.monotonic() + 120
        while True:
            self.disabled()
            health = self.services_health()
            if len(health) == 5 and all(v.get("ok") is True for v in health.values()):
                self.phase("LEGACY_RESTORED_HA_DISABLED")
                return
            require(time.monotonic() < deadline, "Legacy relaunched; readiness not yet confirmed")
            time.sleep(3)

    def enable(self):
        self.passive(maintenance=False, ready=True)
        require(not self.legacy_processes(), "Legacy processes reappeared before HA enable")
        require(self.controller() == {"owner": "comparator", "valid": True}, "Comparator controller required")
        conn = self.connect()
        try:
            self.barrier(conn, timeout=5000)
            count = conn.execute("UPDATE dbo.KPP_HA_Lease WITH(UPDLOCK,HOLDLOCK) SET Enabled=1 "
                                 "WHERE Id=1 AND Enabled=0 AND Owner IS NULL").rowcount
            require(count == 1, "HA lease changed before enable")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.phase("HA_ENABLED_PROTOCOL2")

    def activate(self):
        self.disabled()
        rows = self.passive(maintenance=True)
        require(self.controller() == {"owner": "comparator", "valid": True}, "Comparator controller required")
        for node, s in rows.items():
            require(preflight_ok(s, legacy=node == "physical"), "Unexpected preflight: " + node)
        health = self.services_health()
        require(len(health) == 5 and all(v.get("ok") is True for v in health.values()), "Five legacy services must be ready")
        identities = []
        for key in ("KPP_CONN_STR", "KPP_TASK_CONN_STR"):
            conn = self.driver.connect(os.environ.get(key) or os.environ["KPP_CONN_STR"], timeout=5, autocommit=True)
            try:
                conn.timeout = 5
                identities.append(tuple(conn.execute("SELECT CONVERT(nvarchar(128),SERVERPROPERTY('ServerName')),DB_NAME()").fetchone()))
            finally:
                conn.close()
        require(identities[0] == identities[1], "Separate task output database requires fencing review")
        login = self.migration_login(self.store)
        self.backup = Path(self.cfg["state_dir"]) / "upgrade-backups" / ("cutover-" + uuid.uuid4().hex)
        self.private_directory(self.backup)
        self.save_json(self.backup / "node.json", self.cfg)
        targets = self.capture_legacy()
        self.phase("CUTOVER_PROTOCOL2_PRECHECK_OK")
        print("CUTOVER_BACKUP", str(self.backup), flush=True)
        changed = False
        try:
            self.hold_controller()
            self.passive(maintenance=True)
            import psutil
            for name, p in targets.items():
                self.renew_interlock()
                captured = next(row for row in self.saved if row["wrapper"] == name)
                require(self.same_process(psutil, (captured["pid"], captured["created"]))
                        and p.cmdline() == captured["argv"], "Legacy launcher changed after capture")
                changed = True
                self.stop_tree(psutil, p)
            require(not self.legacy_processes(), "Legacy processes remain")
            self.maintenance(True)  # Now take repair locks and fault ALL three nodes.
            self.migrate(login)
            self.maintenance(False)
            deadline = time.monotonic() + 240
            while True:
                self.renew_interlock()
                try:
                    rows = self.passive(maintenance=False)
                    ready = all(s.get("prepared") is True and s.get("faulted") is False and preflight_ok(s) for s in rows.values())
                    evidence = {n: {k: s.get(k) for k in ("prepared", "faulted", "preflight")} for n, s in rows.items()}
                except Exception as exc:
                    ready, evidence = False, {"error": type(exc).__name__}
                print("INDEPENDENT_RECOVERY_WAIT", json.dumps(evidence), flush=True)
                if ready:
                    break
                require(time.monotonic() < deadline, "Independent recovery not confirmed")
                time.sleep(5)
            self.release_interlock(verify=True)
            self.enable()
            deadline, streak, identity = time.monotonic() + 600, 0, None
            while True:
                lease, rows = {}, {}
                try:
                    lease = self.store.lease()
                    for node in self.nodes:
                        rows[node] = self.status(node)
                    good = cluster_healthy(lease, rows, self.controller())
                except Exception as exc:
                    good = False
                    rows["observation"] = {"error": type(exc).__name__}
                current = (lease.get("owner"), lease.get("epoch"))
                streak = streak + 1 if good and current == identity else int(good)
                identity = current
                print("HA_PROTOCOL2_WAIT", json.dumps({"owner": lease.get("owner"), "epoch": lease.get("epoch"),
                       "valid": lease.get("valid"), "healthy_samples": streak, "nodes": {n: {k: s.get(k)
                       for k in ("active", "healthy", "prepared", "faulted", "error")} for n, s in rows.items()}}), flush=True)
                if streak >= 7:
                    self.phase("HA_ACTIVE_PHYSICAL_RESERVES_READY")
                    return
                require(time.monotonic() < deadline, "Full physical operation not confirmed")
                time.sleep(10)
        except BaseException:
            if changed:
                try:
                    self.rollback()
                except BaseException as recovery:
                    print("ROLLBACK_BLOCKED", str(recovery) if isinstance(recovery, Abort)
                          else type(recovery).__name__, flush=True)
                    print("DO_NOT_START_LEGACY_WITHOUT_CONFIRMED_HA_OFF_AND_EMPTY_WORKERS", flush=True)
            else:
                try:
                    self.release_interlock()
                except Exception:
                    print("CONTROLLER_INTERLOCK_RELEASE_UNCONFIRMED", flush=True)
            raise


def main(argv=None):
    sys.dont_write_bytecode = True
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--activate", action="store_true")
    modes.add_argument("--rollback", type=Path, metavar="BACKUP_DIR")
    args = parser.parse_args(argv)
    app = Cutover()
    from common.single_instance import SingleInstanceLock
    with SingleInstanceLock(str(Path(app.cfg["state_dir"]) / "operator-cutover.lock")):
        if args.rollback:
            backup = args.rollback.resolve()
            root = (Path(app.cfg["state_dir"]) / "upgrade-backups").resolve()
            require(backup.is_relative_to(root) and backup.name.startswith("cutover-"), "Unexpected rollback backup path")
            app.backup = backup
            app.saved = json.loads((backup / "legacy-launchers.json").read_text())
            record = json.loads((backup / "controller.json").read_text())
            app.old_controller, app.operator_token = record["original"], record["operator_token"]
            app.rollback()
        else:
            app.activate()
    return 0


def cli():
    try:
        return main()
    except (Exception, KeyboardInterrupt) as exc:
        print("CUTOVER_PROTOCOL2_STOPPED", str(exc) if isinstance(exc, Abort) else type(exc).__name__, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
