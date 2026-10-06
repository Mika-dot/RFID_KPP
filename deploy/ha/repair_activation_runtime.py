"""Stage a guarded runtime hotfix on each node.

For an existing protocol 2 deployment use --resume-protocol2 from physical:
it verifies the nine existing triggers and releases staged maintenance without
SQL migration. --finalize is the older schema-migration workflow.
"""
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
BASES = (BASE, "d898dbc05feeb3157304d0c34cca344514b59b38",
         "79f79e7ad0836a86808d05b543f14fb9be84e940",
         "79d1caa3a0709ca96d6ee6d55c8ed73623ca8665",
         "f5fdb6aed1c3749b0ada51e28c5dec96ed2c59fa")
PRIORITY = [("physical", 1), ("perimetr", 2), ("comparator", 3)]
OUTPUT_TABLES = ("RFID_Tags", "RusGuardLogs", "ReelTransitions", "KPP_ReelEvents",
                 "KPP_RuntimeState", "KPP_ActiveRfidSessions", "KPP_ProcessingErrors",
                 "KPP_EventVideoLinks", "KPP_EventSkudLinks")
RESUME_PREFLIGHT_TIMEOUT = 180


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


def wait_comparator(store):
    """Keep the stopped Perimetr controller out of the takeover race."""
    deadline = time.monotonic() + 45
    while True:
        with store.connect() as conn:
            row = conn.execute("SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
                               "FROM dbo.KPP_HA_Controller WHERE Id=1").fetchone()
        if row and row[0] == "comparator" and row[1]:
            print("CONTROLLER_ON_COMPARATOR", flush=True)
            return
        require(time.monotonic() < deadline, "Comparator did not take controller ownership")
        time.sleep(2)


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
    result["fencing_protocol"] = status.get("fencing_protocol")
    result["operator_maintenance"] = status.get("operator_maintenance")
    return result


def staged_status(code, status, node, release):
    return (code == 200 and status.get("node") == node
            and status.get("release_sha") == release and status.get("fencing_protocol") == 2
            and status.get("sample_age", 999) < 10 and status.get("active") is False
            and status.get("operator_maintenance") is True)


def wait_staged(get_json, token, node, release):
    deadline = time.monotonic() + 45
    while True:
        try:
            code, status = get_json("http://127.0.0.1:18200/status", token, timeout=5)
            if staged_status(code, status, node, release):
                print("RUNTIME_STAGED", node, release, flush=True)
                return
        except (OSError, ValueError):
            pass
        require(time.monotonic() < deadline, "New agent did not confirm staging")
        time.sleep(2)


def report_errors(cfg, get_json, token):
    for peer in cfg["nodes"]:
        try:
            code, data = get_json(peer["url"].rstrip("/") + "/diagnostics", token, timeout=8)
            print("DIAGNOSTICS", peer["id"], json.dumps(data, ensure_ascii=False), flush=True)
        except Exception as exc:
            print("DIAGNOSTICS_UNAVAILABLE", peer["id"], type(exc).__name__, flush=True)


def migration_connection(store):
    """Find an already configured migration login, before stopping any agent."""
    from guardian.sql import control_odbc
    driver = control_odbc()
    with store.connect() as control:
        target = tuple(control.execute("SELECT CONVERT(nvarchar(128),SERVERPROPERTY('ServerName')),DB_NAME()").fetchone())
    candidates = dict.fromkeys(filter(None, (
        os.getenv("KPP_MIGRATION_TRUSTED_CONN"), os.getenv("KPP_CONN_STR"),
        os.getenv("PERIMETER_HA_SQL"))))
    for candidate in candidates:
        conn = None
        try:
            conn = driver.connect(candidate, timeout=5, autocommit=False)
            conn.timeout = 5
            found = tuple(conn.execute("SELECT CONVERT(nvarchar(128),SERVERPROPERTY('ServerName')),DB_NAME()").fetchone())
            if found != target:
                continue
            if all(conn.execute("SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'ALTER')", "dbo." + table).fetchone()[0] == 1
                   for table in OUTPUT_TABLES):
                print("SQL_MIGRATION_PERMISSION_OK", flush=True)
                return candidate
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()
    raise Abort("No configured login can ALTER the nine HA triggers; SQL migration permission is required")


def check_database_barrier(store):
    """Exercise actual lock compatibility without inserting business data."""
    from guardian.sql import epoch_barrier
    with store.connect() as writer:
        epoch_barrier(writer, "Shared", 0)
        writer.execute("SELECT Epoch FROM dbo.KPP_HA_Lease WITH(READCOMMITTEDLOCK) WHERE Id=1").fetchone()
        with store.connect() as renewal:
            # Preserve the exact expiry and identity; prove that a row renewal
            # can acquire its locks while the writer transaction remains open.
            renewal.execute("UPDATE dbo.KPP_HA_Lease SET ExpiresAt=ExpiresAt WHERE Id=1")
            renewal.commit()
        with store.connect() as transfer:
            try:
                epoch_barrier(transfer, "Exclusive", 0)
            except RuntimeError as exc:
                require(str(exc) == "EpochBarrierUnavailable", "Unexpected SQL barrier error")
            else:
                raise Abort("SQL accepted an epoch change while a writer barrier was held")
        writer.rollback()
    with store.connect() as transfer:
        epoch_barrier(transfer, "Exclusive", 0)
        transfer.rollback()
    print("SQL_EPOCH_BARRIER_CHECK_OK", flush=True)


def finalize(cfg, store, get_json, token, release, source):
    """An atomic migration is permitted only after all three new agents are paused."""
    from guardian.sql import control_odbc, epoch_barrier
    from guardian.config import atomic_json
    from guardian.repair import redact
    snapshots = []
    for peer in cfg["nodes"]:
        code, status = get_json(peer["url"].rstrip("/") + "/status", token, timeout=5)
        require(code == 200 and status.get("node") == peer["id"] and status.get("release_sha") == release
                and status.get("sample_age", 999) < 10 and status.get("fencing_protocol") == 2,
                "Stage the new release on all three nodes first: " + peer["id"])
        snapshots.append((peer, status))
    if not all(status.get("operator_maintenance") is True for _, status in snapshots):
        # Resume a previously committed migration after an interrupted release
        # or repeat verification after an operational readiness failure.
        with store.connect() as control:
            definitions = [control.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?))",
                           "dbo.HA_" + table).fetchone()[0] for table in OUTPUT_TABLES]
        require(all(definition and "Perimeter.HA.Epoch" in definition and "READCOMMITTEDLOCK" in definition
                    and "UPDLOCK" not in definition for definition in definitions),
                "All three agents must be staged before the first SQL migration")
        print("SQL_EPOCH_BARRIER_ALREADY_INSTALLED", flush=True)
        for peer, status in snapshots:
            if status.get("operator_maintenance"):
                code, data = get_json(peer["url"].rstrip("/") + "/maintenance", token, timeout=10,
                                     body={"enabled": False, "release": release})
                require(code == 200 and data.get("operator_maintenance") is False,
                        "Could not resume: " + peer["id"])
        try:
            wait_cluster(cfg, store, get_json, token, release)
        except Exception:
            report_errors(cfg, get_json, token)
            raise
        return
    for peer, status in snapshots:
        require(staged_status(200, status, peer["id"], release), "A staged worker is still active")
        code, diag = get_json(peer["url"].rstrip("/") + "/diagnostics", token, timeout=5)
        require(code == 200 and diag.get("workers") == {}, "Workers were not stopped: " + peer["id"])
    text = migration_connection(store)
    deadline = time.monotonic() + 45
    while True:
        lease = store.lease()
        require(lease["enabled"], "HA must remain enabled")
        if not lease["valid"]:
            break
        require(time.monotonic() < deadline, "Stopped executor lease did not expire")
        time.sleep(2)
    conn = control_odbc().connect(text, timeout=5, autocommit=False)
    backup = Path(cfg["state_dir"]) / "upgrade-backups" / ("sql-epoch-" + uuid.uuid4().hex)
    backup.mkdir(parents=True, mode=0o700)
    try:
        conn.timeout = 10
        conn.execute("SET LOCK_TIMEOUT 5000; SET XACT_ABORT ON;")
        epoch_barrier(conn)
        row = conn.execute("SELECT Enabled,Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
                           "FROM dbo.KPP_HA_Lease WITH(UPDLOCK,HOLDLOCK) WHERE Id=1").fetchone()
        require(row and row[0] and not row[2], "Lease changed before migration")
        originals = {}
        for table in OUTPUT_TABLES:
            row = conn.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?))", "dbo.HA_" + table).fetchone()
            require(row and row[0], "Existing HA trigger is missing: " + table)
            originals[table] = row[0]
        atomic_json(backup / "triggers.json", originals)
        (backup / "triggers.json").chmod(0o600)
        sql = (source / "migrations/004_perimeter_ha_epoch_barrier.sql").read_text(encoding="utf-8-sig")
        cursor = conn.execute(sql)
        while cursor.nextset():
            pass
        for table in OUTPUT_TABLES:
            definition = conn.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?))", "dbo.HA_" + table).fetchone()[0]
            require("Perimeter.HA.Epoch" in definition and "READCOMMITTEDLOCK" in definition
                    and "UPDLOCK" not in definition,
                    "New HA trigger was not verified: " + table)
        conn.commit()
        print("SQL_EPOCH_BARRIER_INSTALLED", "backup=" + str(backup), flush=True)
    except Exception as exc:
        conn.rollback()
        print("SQL_MIGRATION_ROLLED_BACK", redact(str(exc)), flush=True)
        raise
    finally:
        conn.close()
    check_database_barrier(store)
    # Epoch identities remain faulted until independent preflight has passed.
    for peer in sorted(cfg["nodes"], key=lambda n: n["priority"]):
        code, data = get_json(peer["url"].rstrip("/") + "/maintenance", token, timeout=10,
                             body={"enabled": False, "release": release})
        require(code == 200 and data.get("operator_maintenance") is False,
                "Could not resume independent verification: " + peer["id"])
    try:
        wait_cluster(cfg, store, get_json, token, release)
    except Exception:
        report_errors(cfg, get_json, token)
        raise


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
    deadline, streak, identity = time.monotonic() + 600, 0, None
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
        if streak >= 7:
            print("HA_ACTIVE_PHYSICAL_RESERVES_READY", flush=True)
            return
        require(time.monotonic() < deadline, "Full cluster operation has not been confirmed; send this output")
        time.sleep(10)


def wait_resume_agents(cfg, store, get_json, token, release):
    """Wait for asynchronous probes; keep every node held until all checks pass."""
    deadline = time.monotonic() + RESUME_PREFLIGHT_TIMEOUT
    check_names = ("starting", "space", "sql_fencing", "same_output_database",
                   "entrypoints", "configuration", "model", "masks", "dll_file",
                   "ports_free", "sdk_load", "python_dependencies", "exception")
    while True:
        require(store.lease()["enabled"] is True, "HA mode changed; resume stopped")
        snapshots, rows = [], []
        for peer in cfg["nodes"]:
            try:
                code, status = get_json(peer["url"].rstrip("/") + "/status", token, timeout=5)
            except (OSError, ValueError) as exc:
                rows.append({"node": peer["id"], "error": type(exc).__name__})
                continue
            require(code not in (401, 403), "Resume authentication refused: " + peer["id"])
            if code != 200:
                rows.append({"node": peer["id"], "http": code})
                continue
            require(status.get("node") == peer["id"] and status.get("release_sha") == release
                    and status.get("fencing_protocol") == 2
                    and type(status.get("operator_maintenance")) is bool,
                    "All three exact hotfix agents required before resume: " + peer["id"])
            require(not status.get("resources", {}).get("restart_required"),
                    "Agent resource restart required before resume: " + peer["id"])
            if status["operator_maintenance"]:
                code, diag = get_json(peer["url"].rstrip("/") + "/diagnostics", token, timeout=5)
                require(status.get("active") is False and code == 200 and diag.get("workers") == {},
                        "Held executor must be inactive and empty: " + peer["id"])
            preflight = status.get("preflight", {})
            checks = preflight.get("checks", {})
            ready = status.get("sample_age", 999) < 10 and preflight.get("ok") is True
            rows.append({"node": peer["id"], "sample_age": status.get("sample_age"),
                         "preflight_ok": preflight.get("ok") is True,
                         "failed_checks": [name for name in check_names
                                           if name in checks and checks[name] is not True]})
            if ready:
                snapshots.append((peer, status))
        if len(snapshots) == len(cfg["nodes"]):
            print("HOTFIX_ALL_AGENTS_PREFLIGHT_OK", flush=True)
            return snapshots
        print("HOTFIX_RESUME_PREFLIGHT_WAIT", json.dumps(rows), flush=True)
        require(time.monotonic() < deadline,
                "Agent preflight did not become ready; no maintenance was released; see checks above")
        time.sleep(3)


def resume_protocol2(cfg, store, get_json, token, release):
    """Resume a staged hotfix against existing protocol 2; no DDL/migrations."""
    require(store.lease()["enabled"] is True, "Existing enabled protocol 2 HA required")
    with store.connect() as conn:
        for table in OUTPUT_TABLES:
            row = conn.execute("SELECT is_disabled,OBJECT_DEFINITION(object_id) FROM sys.triggers "
                               "WHERE name=?", "HA_" + table).fetchone()
            require(row and not row[0] and row[1] and "Perimeter.HA.Epoch" in row[1]
                    and "READCOMMITTEDLOCK" in row[1] and "UPDLOCK" not in row[1],
                    "Existing protocol 2 trigger required: " + table)
    snapshots = wait_resume_agents(cfg, store, get_json, token, release)
    print("SQL_PROTOCOL2_EXISTING_VERIFIED_NO_MIGRATION", flush=True)
    for peer, status in sorted(snapshots, key=lambda item: item[0]["priority"]):
        require(store.lease()["enabled"] is True, "HA mode changed; resume stopped")
        if status["operator_maintenance"]:
            code, data = get_json(peer["url"].rstrip("/") + "/maintenance", token, timeout=10,
                                 body={"enabled": False, "release": release})
            require(code == 200 and data.get("operator_maintenance") is False,
                    "Independent verification release unconfirmed: " + peer["id"])
            print("HOTFIX_RELEASED_FOR_INDEPENDENT_VERIFICATION", peer["id"], flush=True)
    wait_cluster(cfg, store, get_json, token, release)
    code, status = get_json(next(p["url"] for p in cfg["nodes"] if p["id"] == "physical").rstrip("/")
                            + "/status", token, timeout=5)
    if code == 200:
        for name, health in status.get("services", {}).items():
            warnings = health.get("detail", {}).get("warnings")
            if warnings:
                # Only a typed allowlisted reason; never export arbitrary data.
                flow = warnings.get("business_flow", {})
                if name == "RfidReader" and flow.get("detail") in {"rfid_stale_with_partial_activity_evidence", "rfid_historical_activity_evidence_contradicted"}:
                    print("RFID_BUSINESS_WARNING", flow["detail"], "; real RFID passage still required", flush=True)
    print("HOTFIX_PROTOCOL2_RUNTIME_RESTORED; BUSINESS_READ_ACCEPTANCE_NOT_PROVEN", flush=True)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--node", required=True, choices=("physical", "perimetr", "comparator"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stage", action="store_true")
    mode.add_argument("--finalize", action="store_true")
    mode.add_argument("--diagnose", action="store_true")
    mode.add_argument("--resume-protocol2", action="store_true")
    parser.add_argument("--resume-after-stage", action="store_true",
                        help="Perimetr only: after staging all three exact agents, release protocol 2 and verify the cluster")
    args = parser.parse_args(argv)
    require(not args.resume_after_stage or (args.stage and args.node == "perimetr"),
            "Resume-after-stage requires the final Perimetr staging step")
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
    if args.diagnose:
        report_errors(cfg, get_json, token)
        return 0
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
    def resume_after_stage():
        if args.resume_after_stage:
            with SingleInstanceLock(str(Path(cfg["state_dir"]) / "operator-cutover.lock")):
                resume_protocol2(cfg, store, get_json, token, args.release)
    if args.resume_protocol2:
        require(windows and args.node == "physical", "Resume protocol 2 from physical only")
        require(state["current"]["sha"] == args.release and state.get("fencing_protocol_min") == 2,
                "Stage this exact protocol 2 hotfix on physical first")
        with SingleInstanceLock(str(Path(cfg["state_dir"]) / "operator-cutover.lock")):
            resume_protocol2(cfg, store, get_json, token, args.release)
        return 0
    if args.finalize:
        require(state["current"]["sha"] == args.release, "Stage this node first")
        finalize(cfg, store, get_json, token, args.release, source)
        return 0
    if windows:
        migration_connection(store)  # Detect missing DDL permissions BEFORE a rollout.
    if args.node == "perimetr":
        for peer in cfg["nodes"]:
            if peer["id"] != "perimetr":
                code, status = get_json(peer["url"].rstrip("/") + "/status", token, timeout=5)
                require(staged_status(code, status, peer["id"], args.release),
                        "Stage physical and Comparator before Perimetr: " + peer["id"])
    if state["current"]["sha"] == args.release:
        wait_staged(get_json, token, args.node, args.release)
        if args.node == "perimetr":
            with store.connect() as conn:
                controller = conn.execute("SELECT Owner,CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END "
                                          "FROM dbo.KPP_HA_Controller WHERE Id=1").fetchone()
            if not (controller and controller[0] == "comparator" and controller[1]):
                try:
                    stop_guardian()
                    wait_comparator(store)
                finally:
                    start_guardian(False)
                wait_staged(get_json, token, args.node, args.release)
        resume_after_stage()
        return 0
    require(state["current"]["sha"] in BASES, "Unexpected runtime release; nothing was stopped")
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
    if state["current"]["sha"] == "f5fdb6aed1c3749b0ada51e28c5dec96ed2c59fa":
        for pattern in ("test_rfid_evidence_ordering.py", "test_business_flow_selfheal.py", "test_rfid_advisory_readiness.py"):
            require((candidate / "tests" / pattern).is_file(), "Business-flow candidate regressions required")
            tests = subprocess.run([cfg["python"], "-B", "-m", "unittest", "discover", "-s", "tests", "-p", pattern],
                                   cwd=candidate, stdin=subprocess.DEVNULL, timeout=60)
            require(tests.returncode == 0, "Business-flow regression tests failed; nothing was stopped")
    if state["current"]["sha"] == "79d1caa3a0709ca96d6ee6d55c8ed73623ca8665":
        require((candidate / "tests/test_rfid_advisory_readiness.py").is_file(),
                "RFID warning regression tests required for this hotfix")
        tests = subprocess.run([cfg["python"], "-B", "-m", "unittest", "discover", "-s", "tests",
                                "-p", "test_rfid_advisory_readiness.py"], cwd=candidate,
                               stdin=subprocess.DEVNULL, timeout=60)
        require(tests.returncode == 0, "RFID warning regression tests failed; nothing was stopped")
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
            state["fencing_protocol_min"] = 2
            write_record(record, state)
            write_record(Path(cfg["state_dir"]) / "repair-verification.json", {"required": True})
            write_record(Path(cfg["state_dir"]) / "operator-maintenance.json", {"enabled": True})
            print("HOTFIX_INSTALLED", args.node, args.release, flush=True)
        if args.node == "perimetr":
            wait_comparator(store)
    finally:
        if restart:
            start_guardian(windows)
    wait_staged(get_json, token, args.node, args.release)
    resume_after_stage()
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("HOTFIX_FAILED", type(exc).__name__, str(exc) if isinstance(exc, Abort) else "", flush=True)
        raise SystemExit(2)
