"""Read-only HA rollout evidence. Does not start services, load the SDK or write SQL.

Run outside the installed checkout with its existing native Python. Only the
sanitized --output report is written. Config/secrets are read locally, never
printed. Compatible with Python 3.10 on Windows and Ubuntu.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SERVICES = dict(zip(("RfidReader", "RusGuardSync", "Yolo", "Aggregator", "WebDashboard"),
                    range(18101, 18106)))
TABLES = ("RFID_Tags", "RusGuardLogs", "ReelTransitions", "KPP_ReelEvents",
          "KPP_RuntimeState", "KPP_ActiveRfidSessions", "KPP_ProcessingErrors",
          "KPP_EventVideoLinks", "KPP_EventSkudLinks")
SENSITIVE = re.compile(r"PASSWORD|TOKEN|SECRET|CONNECTION|CONN_STR|SQL_CONN|AUTH_USER|USERNAME", re.I)


def error(exc):
    # Driver messages can contain full connection strings. Preserve only class
    # and recognized numeric error codes; no arbitrary exception text.
    codes = sorted(set(re.findall(r"\b(?:15664|5100[1-4]|18456|4060)\b", str(exc))))
    return {"error": type(exc).__name__, "sql_codes": codes}


def safe_call(fn):
    try:
        return fn()
    except Exception as exc:
        return error(exc)


def scrub(value, env):
    secrets = {v for k, v in env.items() if SENSITIVE.search(k) and isinstance(v, str) and v}
    # ODBC escaping can make the password spelling differ from its raw value.
    secrets.update(v.replace("}", "}}") for v in list(secrets))
    ordered = sorted(secrets, key=len, reverse=True)
    def visit(item):
        if isinstance(item, dict):
            return {str(k): ("REDACTED" if SENSITIVE.search(str(k)) or str(k).lower() in
                            ("authorization", "pwd", "uid") else visit(v)) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [visit(v) for v in item]
        if not isinstance(item, (str, int, float, bool, type(None))):
            item = str(item)
        if isinstance(item, str):
            for secret in ordered:
                item = item.replace(secret, "REDACTED")
            item = re.sub(r"(?i)(?:rtsp|https?)://[^\s/@]+@", "URL://REDACTED@", item)
        return item
    return visit(value)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def command(argv, timeout=10):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout)
    # stderr may include credentials from a remote URL. Never return it.
    return {"exit_code": result.returncode, "stdout": result.stdout.strip()}


def request_json(url, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        response = opener.open(req, timeout=4)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("OversizedResponse")
        return {"http": response.status, "body": json.loads(raw)}


def git_snapshot(root):
    git = "git"
    if os.name == "nt" and Path("C:/Program Files/Git/cmd/git.exe").is_file():
        git = "C:/Program Files/Git/cmd/git.exe"
    prefix = [git, "--no-optional-locks", "-c", "safe.directory=" + str(root), "-C", str(root)]
    return {"root": str(root),
            "head": safe_call(lambda: command(prefix + ["rev-parse", "HEAD"])),
            "changes": safe_call(lambda: command(prefix + ["status", "--porcelain", "--untracked-files=normal"]))}


def protocol(root):
    tree = ast.parse((root / "guardian/sql.py").read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if any(isinstance(t, ast.Name) and t.id == "FENCING_PROTOCOL" for t in node.targets):
                return node.value.value
    return 1


def native_state():
    if os.name != "nt":
        return command(["systemctl", "show", "perimeter-guardian.service", "--no-pager",
                        "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
                        "-p", "UnitFileState", "-p", "MainPID", "-p", "ControlPID", "-p", "ControlGroup"])
    script = ("$ErrorActionPreference='Stop';"
              "$t=Get-ScheduledTask -TaskName PerimeterGuardian;"
              "$i=Get-ScheduledTaskInfo -TaskName PerimeterGuardian;"
              "@{state=[string]$t.State;enabled=$t.Settings.Enabled;"
              "last_result=$i.LastTaskResult;last_run=[string]$i.LastRunTime} | ConvertTo-Json -Compress")
    env = os.environ.copy()
    env["PSModulePath"] = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/Modules")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=15)
    return {"exit_code": result.returncode,
            "task": json.loads(result.stdout) if result.returncode == 0 else None}


def process_snapshot(roots):
    import psutil
    rows, inaccessible = [], 0
    candidates = {"python", "python3", "python.exe", "pythonw.exe", "cmd.exe", "powershell.exe",
                  "wine", "wine-preloader", "wine64", "wine64-preloader", "xvfb-run"}
    normalized_roots = [str(r).replace("\\", "/").casefold().rstrip("/") for r in roots]
    for p in psutil.process_iter():
        try:
            name = p.name()
            if name.lower() not in candidates and not name.lower().startswith("python3."):
                continue
            args = p.cmdline()
            normalized = [a.replace("\\", "/").casefold() for a in args]
            if not any("guardian" in a or "wine_worker.py" in a or
                       any(r + "/" in a for r in normalized_roots) for a in normalized):
                continue
            # Return identifiable script paths, NEVER complete argv or env.
            entrypoints = [a for a in args[1:] if a.lower().endswith((".py", ".cmd", ".ps1"))
                          and any(a.replace("\\", "/").casefold().startswith(r + "/") for r in normalized_roots)]
            rows.append({"pid": p.pid, "parent": p.ppid(), "created": p.create_time(),
                         "name": name, "entrypoints": entrypoints,
                         "guardian_module": any(args[i:i+2] == ["-m", "guardian"] for i in range(len(args)-1))})
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            inaccessible += 1
    return {"processes": rows, "inaccessible_candidates": inaccessible}


def sql_snapshot(env):
    import pyodbc
    pyodbc.pooling = False
    conn = pyodbc.connect(env["PERIMETER_HA_SQL"], timeout=4, autocommit=True)
    try:
        conn.timeout = 4
        def rows(query):
            cur = conn.execute(query)
            keys = [d[0] for d in cur.description]
            return [dict(zip(keys, tuple(r))) for r in cur.fetchall()]
        result = {"clock": rows("SELECT SYSUTCDATETIME() AS utc,DB_NAME() AS database_name"),
                  "lease": safe_call(lambda: rows("SELECT Enabled,Owner,Epoch,StartedAt,ExpiresAt,"
                    "CASE WHEN Enabled=1 AND ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END AS Valid "
                    "FROM dbo.KPP_HA_Lease WHERE Id=1")),
                  "controller": safe_call(lambda: rows("SELECT Owner,ExpiresAt,"
                    "CASE WHEN ExpiresAt>SYSUTCDATETIME() THEN 1 ELSE 0 END AS Valid "
                    "FROM dbo.KPP_HA_Controller WHERE Id=1")),
                  "quarantine": safe_call(lambda: rows("SELECT NodeId,Faulted,VerifiedAt FROM dbo.KPP_HA_NodeState")),
                  "triggers": safe_call(lambda: rows("SELECT name,is_disabled,"
                    "CASE WHEN OBJECT_DEFINITION(object_id) IS NULL THEN NULL "
                    "WHEN OBJECT_DEFINITION(object_id) LIKE '%Perimeter.HA.Epoch%' THEN 2 ELSE 1 END AS protocol "
                    "FROM sys.triggers WHERE name LIKE 'HA[_]%'")),
                  "cursors": safe_call(lambda: rows("SELECT StateKey,StateValue FROM dbo.KPP_RuntimeState "
                    "WHERE StateKey IN ('LAST_RFID_ID_V3','LAST_WAREHOUSE_ID_V3_4_5_RECHECK',"
                    "'LAST_RUSGUARD_EXTERNAL_ID_V2','KPP_SCHEMA_VERSION')")),
                  "active_sessions": safe_call(lambda: rows("SELECT COUNT_BIG(*) AS count FROM dbo.KPP_ActiveRfidSessions"))}
        result["migration_permissions"] = [
            {"table": table, "alter": conn.execute("SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'ALTER')", "dbo." + table).fetchone()[0]}
            for table in TABLES]
        return result
    finally:
        conn.close()


def spool_snapshot(path, kind):
    path = path.resolve()
    if not path.is_file():
        return {"path": str(path), "exists": False}
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        conn.execute("PRAGMA query_only=ON")
        # No journal_mode, checkpoint, retention or writer operations.
        table = "reads" if kind == "rfid" else "events"
        counts = conn.execute("SELECT state,COUNT(*) FROM " + table + " GROUP BY state").fetchall()
        return {"path": str(path), "exists": True, "bytes": path.stat().st_size, "counts": dict(counts)}
    finally:
        conn.close()


def release_snapshot(state):
    result = {}
    for name in ("release.json", "operator-maintenance.json", "repair-verification.json"):
        path = state / name
        if not path.exists():
            result[name] = {"exists": False}
            continue
        value = read_json(path)
        if name == "release.json":
            value = {k: value.get(k) for k in ("current", "previous", "pending", "trial_started",
                                              "fencing_protocol_min", "trusted_main_sha")}
            for k in ("current", "previous"):
                if isinstance(value[k], dict):
                    value[k] = {x: value[k].get(x) for x in ("root", "sha", "python")}
        else:
            value = {"enabled": value.get("enabled"), "required": value.get("required")}
        result[name] = value
    return result


def local_logs(state, env):
    result = {}
    for service in SERVICES:
        path = state / "logs" / (service + ".log")
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            # Drop the first partial line: a truncated password cannot be
            # scrubbed by matching its complete value.
            start = max(0, path.stat().st_size - 4096)
            stream.seek(start)
            raw = stream.read(4096)
        if start:
            raw = raw.split(b"\n", 1)[1] if b"\n" in raw else b""
        lines = raw.decode("utf-8", "replace").splitlines()
        # Exclude lines containing unknown credential assignments/URLs even
        # after exact-value redaction. Keep traceback frames and error codes.
        safe = []
        for line in lines:
            line = scrub(line, env)
            if re.search(r"(?i)(?:password|pwd|token|secret|authorization|uid)\s*[=:]|(?:rtsp|https?)://", line):
                continue
            safe.append(line)
        result[service] = "\n".join(safe)
    return result


def collect(cfg, env):
    root, state = Path(cfg["root"]), Path(cfg["state_dir"])
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "host": socket.gethostname(),
              "node": cfg["node_id"], "python": sys.executable, "read_only": True,
              "source": git_snapshot(root), "source_protocol": safe_call(lambda: protocol(root)),
              "state": safe_call(lambda: release_snapshot(state)), "native": safe_call(native_state),
              "sql": safe_call(lambda: sql_snapshot(env))}
    roots = [root]
    record = safe_call(lambda: read_json(state / "release.json"))
    if isinstance(record, dict) and isinstance(record.get("current"), dict):
        roots.append(Path(record["current"]["root"]))
    if os.name == "nt":
        legacy = Path("D:/Desktop/RFID_KPP-main")
        roots.append(legacy)
        report["legacy_source"] = git_snapshot(legacy)
        rollback = Path("D:/PerimeterHA/return-legacy-tonight.py")
        report["rollback_helper"] = {"exists": rollback.is_file(),
            "sha256": hashlib.sha256(rollback.read_bytes()).hexdigest() if rollback.is_file() else None}
    report["processes"] = safe_call(lambda: process_snapshot(roots))
    jobs = [("service:" + n, "http://127.0.0.1:%d/health/ready" % p, None) for n, p in SERVICES.items()]
    jobs += [("agent:" + peer["id"], peer["url"].rstrip("/") + "/status", env.get("PERIMETER_HA_TOKEN"))
             for peer in cfg["nodes"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda item: safe_call(lambda: request_json(item[1], item[2])), jobs))
    report["http"] = dict(zip((item[0] for item in jobs), responses))
    report["logs"] = safe_call(lambda: local_logs(state, env))
    report["spools"] = {}
    for key, kind in (("RFID_SPOOL_PATH", "rfid"), ("RFID_VIDEO_SPOOL", "video")):
        if env.get(key):
            path = Path(env[key])
            if not path.is_absolute():
                path = root / path
            report["spools"][key] = safe_call(lambda: spool_snapshot(path, kind))
    return scrub(report, env)


def main(argv=None):
    sys.dont_write_bytecode = True
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", choices=("physical", "perimetr", "comparator"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    config = Path("D:/PerimeterHA/node.json" if os.name == "nt" else "/etc/perimeter/node.json")
    cfg = read_json(config)
    if cfg["node_id"] != args.node:
        raise ValueError("UnexpectedNodeIdentity")
    sys.path.insert(0, str(Path(cfg["root"]) / "deploy/ha"))
    env = os.environ.copy()
    if os.name == "nt":
        from windows_tool import read_bundle, windows_environment
        env.update(windows_environment(read_bundle("D:/PerimeterHA/transfer-private/environment.local.json")))
    else:
        from environment_tool import read_generated_environment
        env.update(read_generated_environment("/etc/perimeter/environment"))
    env.update(cfg.get("env", {}))
    report = collect(cfg, env)
    # Never overwrite source/config/state/spools or an earlier evidence report.
    output = args.output.resolve()
    with output.open("x", encoding="utf-8") as stream:
        if os.name != "nt":
            os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("HA_INSPECTION_SAVED " + str(output), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps(error(exc)), file=sys.stderr)
        raise SystemExit(2)
