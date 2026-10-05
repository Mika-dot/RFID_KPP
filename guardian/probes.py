from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from guardian.config import SERVICES
from guardian.sql import control_odbc


def get_json(url, token=None, timeout=3, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        response = e
    with response:
        raw = response.read(256*1024 + 1)
        if len(raw) > 256*1024:
            raise ValueError("Oversized response")
        return response.status, json.loads(raw)


def services_health():
    def probe(item):
        name, (port, _) = item
        try:
            code, data = get_json("http://127.0.0.1:%d/health/ready" % port)
            return name, {"ok": code == 200 and data.get("status") == "ok", "detail": data}
        except Exception as e:
            return name, {"ok": False, "error": type(e).__name__}
    with ThreadPoolExecutor(max_workers=5) as pool:
        return dict(pool.map(probe, SERVICES.items()))


def preflight(cfg, store, active=False):
    checks = {}
    root = Path(cfg["root"])
    try:
        checks["space"] = shutil.disk_usage(cfg["state_dir"]).free >= cfg.get("min_free_bytes", 2*1024**3)
        with store.connect() as conn:
            identity = conn.execute("SELECT CONVERT(nvarchar(128),SERVERPROPERTY('ServerName')),DB_NAME()").fetchone()
            row = conn.execute("""
SELECT COUNT(*) FROM sys.triggers WHERE name IN
('HA_RFID_Tags','HA_RusGuardLogs','HA_ReelTransitions','HA_KPP_ReelEvents',
'HA_KPP_RuntimeState','HA_KPP_ActiveRfidSessions','HA_KPP_ProcessingErrors',
'HA_KPP_EventVideoLinks','HA_KPP_EventSkudLinks') AND is_disabled=0
""").fetchone()
            checks["sql_fencing"] = row[0] == 9
        pyodbc = control_odbc()
        env = os.environ.copy()
        env.update(cfg.get("env", {}))
        env.update(PERIMETER_HA_NODE=cfg["node_id"], PERIMETER_HA_STATE_DIR=cfg["state_dir"])
        checks["same_output_database"] = True
        connections = [env.get(key, "") for key in (
            "RFID_DB_CONNECTION", "KPP_CONN_STR", "KPP_WEB_DB_CONNECTION")]
        def odbc_value(value):
            return "{" + value.replace("}", "}}") + "}"
        # Use the same DST connection definition as DB_RusGard/db_sync_v2.py.
        connections.append("DRIVER=%s;SERVER=%s;DATABASE=%s;UID=%s;PWD=%s;Encrypt=yes;TrustServerCertificate=yes;" %
            tuple(odbc_value(env.get(key, default)) for key, default in (
                ("DST_DRIVER", "ODBC Driver 18 for SQL Server"), ("DST_SERVER", ""),
                ("DST_DATABASE", ""), ("DST_USERNAME", ""), ("DST_PASSWORD", ""))))
        for connection in connections:
            candidate = pyodbc.connect(connection, timeout=3, autocommit=True)
            try:
                candidate.timeout = 3
                found = candidate.execute("SELECT CONVERT(nvarchar(128),SERVERPROPERTY('ServerName')),DB_NAME()").fetchone()
                if tuple(identity) != tuple(found):
                    checks["same_output_database"] = False
            finally:
                candidate.close()
        checks["entrypoints"] = all((root / script).is_file() for _, script in SERVICES.values())
        checks["configuration"] = all(env.get(k) for k in (
            "RFID_DB_CONNECTION", "KPP_CONN_STR", "RFID_READER_IP", "RFID_DLL_PATH",
            "SRC_SERVER", "SRC_DATABASE", "SRC_USERNAME", "SRC_PASSWORD",
            "DST_SERVER", "DST_DATABASE", "DST_USERNAME", "DST_PASSWORD",
            "RFID_RTSP_0", "RFID_RTSP_1", "KPP_WEB_AUTH_USER", "KPP_WEB_AUTH_PASSWORD"))
        checks["model"] = Path(env.get("RFID_MODEL_PATH", "__missing__")).is_file()
        checks["masks"] = env.get("RFID_MASK_ENABLED", "1") == "0" or all(
            Path(env.get("RFID_MASK_"+str(i), "__missing__")).is_file() for i in range(2))
        checks["dll_file"] = Path(env.get("RFID_DLL_PATH", "__missing__")).is_file()
        if not active:
            checks["ports_free"] = True
            for port, _ in SERVICES.values():
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        checks["ports_free"] = False
                except OSError:
                    pass
            # This does not connect to the reader or issue inventory commands.
            py = cfg.get("python32", cfg["python"])
            code = "from RFID_reader_v4.rfid_to_sql_v4 import load_library; load_library()"
            r = subprocess.run([py, "-c", code], cwd=root, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
            checks["sdk_load"] = r.returncode == 0
        else:
            checks["sdk_load"] = True
        # Validate dependencies on each native interpreter, without importing a model.
        r = subprocess.run([cfg["python"], "-c", "import pyodbc,flask,waitress,numpy,cv2,ultralytics"],
                           cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        checks["python_dependencies"] = r.returncode == 0
    except Exception as e:
        checks["exception"] = type(e).__name__
    return {"ok": bool(checks) and all(v is True for v in checks.values()), "checks": checks}
