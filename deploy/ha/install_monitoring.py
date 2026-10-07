"""Install an independent ub22 HA observer and existing Grafana panels.

Use finish_monitoring.py for direct Zabbix API setup after installation.
Grafana Zabbix proxy permits reads only; never send item/trigger writes there.

Self-contained stdlib tool. Does not change HA, SQL, worker processes or main.
Grafana token is read locally only during installation, never copied to daemon.
"""
import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

NODES = {"physical": "172.31.0.188", "perimetr": "172.31.0.134", "comparator": "172.31.0.192"}
LABELS = {"physical": "Физика", "perimetr": "Perimetr", "comparator": "Comparator"}
MARKER = "Managed by Perimeter HA external observer v1"
UNIT = "perimeter-ha-monitor.service"
SCRIPT = Path("/usr/local/lib/perimeter-ha-monitor/monitor.py")
BASE = "http://127.0.0.1:19152"
WALLBOARD_BASE = "http://127.0.0.1:19150/perimeter-ha"
DS = {"type": "yesoreyeram-infinity-datasource", "uid": "wallboard-api"}
KEY = "perimeter.ha.cluster.json"


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_json(url, token=None, body=None, method=None, accept_degraded=False, ha_token=None):
    if token and urlsplit(url).netloc != "127.0.0.1:3000":
        raise ValueError("CredentialDestinationRefused")
    if ha_token and (token or url not in {"http://"+ip+":18200/status" for ip in NODES.values()}):
        raise ValueError("CredentialDestinationRefused")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if ha_token:
        headers["Authorization"] = "Bearer " + ha_token
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        response = opener.open(req, timeout=4)
    except HTTPError as exc:
        if not accept_degraded or exc.code != 503:
            raise
        response = exc
    with response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("ResponseTooLarge")
        return response.code, json.loads(raw)


def error_code(exc):
    if isinstance(exc, HTTPError):
        return "HTTP_" + str(exc.code)
    if isinstance(exc, URLError):
        return "NETWORK_" + type(exc.reason).__name__
    if isinstance(exc, (RuntimeError, ValueError)):
        reason = str(exc)
        safe = {"NodeIdentityMismatch", "InvalidHealthSchema", "InvalidEpoch", "InvalidBusinessSchema", "ResponseTooLarge",
                "ExistingGrafanaTokenMissing", "InvalidDatasourceUid", "RunOnUb22WithSudo", "ExistingDashboardNotWritable",
                "ExpectedExistingInfinityDatasourceRequired", "ExistingWallboardSourceMissing", "WallboardHttpServerAdapterUnsupported",
                "ExistingWallboardDropinCollision", "ExistingWallboardLauncherUnexpected", "ExistingWallboardProjectsContractUnexpected",
                "ExpectedExistingZabbixServerHostRequired", "DashboardPanelIdCollision", "ExistingUnitCollision", "ExistingObserverFileCollision",
                "ObserverInitialPollNotConfirmed", "WallboardAdapterNotConfirmed", "ExistingItemCollision", "ExistingTriggerCollision",
                "DashboardSaveNotConfirmed", "GrafanaBackendQueryNotConfirmed", "ZabbixHistoryNotConfirmed", "ObserverAutostartNotConfirmed",
                "NativeCommandFailed", "CredentialDestinationRefused", "TransferObserverTokenFromPhysicalFirst", "InvalidObserverToken", "AgentChangedDuringProbe"}
        if reason in safe or re.fullmatch(r"ZabbixApiRejected_(?:host_get|item_get|item_create|item_update|trigger_get|trigger_create|trigger_update)", reason):
            return reason
    return type(exc).__name__


def probe(pair):
    node, ip = pair
    result = {"node": node, "label": LABELS[node], "reachable": 0, "active": 0,
              "healthy": 0, "prepared": 0, "faulted": 0, "epoch": 0, "http": 0,
              "severity": 1, "role": "НЕДОСТУПЕН", "detail": "Нет ответа агента"}
    try:
        code, data = http_json("http://" + ip + ":18200/health/ready", accept_degraded=True)
        if not isinstance(data, dict) or data.get("node") != node:
            raise ValueError("NodeIdentityMismatch")
        for key in ("active", "healthy", "prepared", "faulted"):
            if type(data.get(key)) is not bool:
                raise ValueError("InvalidHealthSchema")
        if type(data.get("epoch")) is not int or data["epoch"] < 0:
            raise ValueError("InvalidEpoch")
        result.update({k: int(data[k]) for k in ("active", "healthy", "prepared", "faulted", "epoch")})
        result.update(reachable=1, http=code)
        if result["active"]:
            ok = result["healthy"] and not result["faulted"] and code == 200
            result.update(role="ВЕДУЩИЙ", severity=0 if ok else 2, detail="Стек готов" if ok else "Ведущий не готов")
        else:
            ok = result["prepared"] and not result["faulted"] and code == 200
            result.update(role="РЕЗЕРВ", severity=0 if ok else 1, detail="Резерв готов" if ok else "Резерв не готов / ремонт")
        token = observer_token()
        if token:
            _, full = http_json("http://"+ip+":18200/status", ha_token=token)
            if full.get("node") != node:
                raise ValueError("NodeIdentityMismatch")
            if full.get("epoch") != result["epoch"] or any(type(full.get(k)) is not bool or int(full[k]) != result[k] for k in ("active", "healthy", "prepared", "faulted")):
                raise ValueError("AgentChangedDuringProbe")
            sha = full.get("release_sha", "")
            result["release"] = sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) else "НЕИЗВЕСТНО"
            age = full.get("sample_age")
            if type(age) not in (int, float) or not 0 <= age <= 20:
                result.update(severity=2 if result["active"] else 1, detail="Снимок агента устарел")
            elif not result["healthy"] and result["active"]:
                services = full.get("services", {})
                bad = [name for name in ("RfidReader", "RusGuardSync", "Yolo", "Aggregator", "WebDashboard") if not services.get(name, {}).get("ok")]
                result["detail"] = "Не готовы: " + ", ".join(bad) if bad else "Ведущий не готов"
            failed = [key for key, value in full.get("preflight", {}).get("checks", {}).items() if value is False and key in ("space", "sql_fencing", "same_output_database", "entrypoints", "configuration", "model", "masks", "dll_file", "ports_free", "sdk_load", "python_dependencies")]
            if failed:
                result["detail"] += "; preflight: " + ", ".join(failed)
            detail = full.get("services", {}).get("RfidReader", {}).get("detail", {})
            result["business_level"], result["business"] = business_status(detail)
    except Exception as exc:
        result["severity"] = 2 if result["active"] else 1
        result["detail"] = error_code(exc)
    return result


def observer_token():
    folder = os.environ.get("CREDENTIALS_DIRECTORY")
    path = Path(folder) / "ha-token" if folder else Path("/etc/perimeter-ha-monitor.token")
    if not folder and os.geteuid() != 0:
        return None
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("InvalidObserverToken")
    return value


def business_status(data):
    flow = data.get("dependencies", {}).get("business_flow", {})
    status = flow.get("status")
    if status == "unavailable":
        return 2, "RFID не готов"
    if status == "degraded" or data.get("warnings", {}).get("business_flow"):
        return 1, "Предупреждение RFID-потока"
    if status == "ok":
        return 0, "Без предупреждений"
    return 1, "Бизнес-статус неизвестен"


def business_probe(node):
    try:
        code, data = http_json("http://" + NODES[node] + ":18101/health/ready", accept_degraded=True)
        if code != 200:
            return 2, "RFID не готов"
        return business_status(data)
    except Exception as exc:
        return 1, "Бизнес-проверка: " + error_code(exc)


def summarize(nodes, business=(1, "Нет готового ведущего")):
    active = [n for n in nodes if n["reachable"] and n["active"]]
    reserves = sum(int(n["reachable"] and not n["active"] and n["prepared"] and not n["faulted"] and n["http"] == 200) for n in nodes)
    healthy = len(active) == 1 and active[0]["severity"] == 0
    severity = 2 if not healthy else max(business[0], int(reserves != 2))
    detail = "Ведущий и два резерва готовы" if severity == 0 else "; ".join(n["label"] + ": " + n["detail"] for n in nodes if n["severity"])
    if len(active) > 1:
        detail = "Несколько активных узлов — проверить fencing"
    elif not active:
        detail = "Нет доступного активного узла"
    if business[0]:
        detail += ("; " if detail else "") + business[1]
    return {"timestamp": int(time.time()), "severity": severity, "active_count": len(active),
            "ready_reserves": reserves, "business_level": business[0], "business": business[1],
            "leader": active[0]["label"] if len(active) == 1 else "НЕ ОПРЕДЕЛЁН",
            "detail": detail, "nodes": {n["node"]: n for n in nodes}}


def collect():
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        nodes = list(pool.map(probe, NODES.items()))
    active = [n for n in nodes if n["active"] and n["reachable"] and n["severity"] == 0]
    if len(active) == 1:
        owner = active[0]
        business = (owner["business_level"], owner["business"]) if "business_level" in owner else business_probe(owner["node"])
    else:
        business = (1, "Нет готового ведущего")
    return summarize(nodes, business)


def freshness(data, now=None):
    value = copy.deepcopy(data)
    if (time.time() if now is None else now) - value["timestamp"] > 25:
        value.update(severity=2, detail="Данные наблюдателя устарели", business_level=1, business="НЕИЗВЕСТНО")
        for node in value["nodes"].values():
            node.update(severity=2, role="НЕИЗВЕСТНО", detail="Устаревшие данные")
    return value


def serve():
    state = {"data": summarize([probe_result(n) for n in NODES]), "lock": threading.Lock()}
    def update():
        while True:
            start = time.monotonic()
            try:
                value = collect()
                with state["lock"]:
                    state["data"] = value
            except Exception as exc:
                print("MONITOR_POLL_ERROR", error_code(exc), flush=True)
            time.sleep(max(.2, 5 - (time.monotonic() - start)))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with state["lock"]:
                data = freshness(state["data"])
            if self.path == "/status":
                value = data
            elif self.path == "/summary":
                value = [{k: v for k, v in data.items() if k != "nodes"}]
            elif self.path == "/nodes":
                value = list(data["nodes"].values())
            else:
                self.send_error(404)
                return
            raw = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 19152), Handler)
    server.daemon_threads = True
    threading.Thread(target=update, daemon=True).start()
    server.serve_forever()


def proxy_wallboard(source):
    """Keep the original wallboard code/env; add only HA routes on allowed19150."""
    import http.server
    import runpy
    def wrapped(server_class):
        class Server(server_class):
            def __init__(self, address, handler, *args, **kwargs):
                class Handler(handler):
                    def do_GET(self):
                        if self.path in ("/perimeter-behavior/status","/perimeter-behavior/summary","/perimeter-behavior/metrics"):
                            try:
                                value=http_json("http://127.0.0.1:19153/status",ha_token=observer_token())[1]
                            except Exception:
                                value={"status":"collector_error","stale":True,"metrics":{}}
                            if self.path.endswith("/summary"):
                                value=[{k:v for k,v in value.items() if k not in {"metrics","hypotheses"}}]
                            elif self.path.endswith("/metrics"):
                                value=[dict(metric=k,**v) for k,v in value.get("metrics",{}).items()]
                            raw=json.dumps(value,ensure_ascii=False).encode()
                            self.send_response(200);self.send_header("Content-Type","application/json")
                            self.send_header("Content-Length",str(len(raw)));self.send_header("Cache-Control","no-store")
                            self.end_headers();self.wfile.write(raw);return
                        if self.path not in ("/perimeter-ha/status", "/perimeter-ha/summary", "/perimeter-ha/nodes"):
                            return super().do_GET()
                        route = self.path.removeprefix("/perimeter-ha")
                        try:
                            value = http_json(BASE + route)[1]
                        except Exception:
                            data = summarize([probe_result(n) for n in NODES])
                            data.update(severity=2, detail="Наблюдатель HA недоступен")
                            value = data if route == "/status" else list(data["nodes"].values()) if route == "/nodes" else [{k: v for k, v in data.items() if k != "nodes"}]
                        raw = json.dumps(value, ensure_ascii=False).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(raw)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(raw)
                super().__init__(address, Handler, *args, **kwargs)
        return Server
    http.server.HTTPServer = wrapped(http.server.HTTPServer)
    http.server.ThreadingHTTPServer = wrapped(http.server.ThreadingHTTPServer)
    sys.argv = [str(source)]
    sys.path.insert(0, str(Path(source).parent))
    runpy.run_path(str(source), run_name="__main__")


def probe_result(node):
    return {"node": node, "label": LABELS[node], "reachable": 0, "active": 0,
            "healthy": 0, "prepared": 0, "faulted": 0, "epoch": 0, "http": 0,
            "severity": 1, "role": "ЗАПУСК", "detail": "Ожидание первой проверки"}


def read_env():
    values = {}
    for line in Path("/etc/mositlab-wallboard.env").read_text().splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and key in ("GRAFANA_TOKEN", "ZABBIX_UID"):
            parts = shlex.split(value, comments=True)
            if len(parts) == 1:
                values[key] = parts[0]
    if not values.get("GRAFANA_TOKEN"):
        raise ValueError("ExistingGrafanaTokenMissing")
    uid = values.get("ZABBIX_UID", "bfvr5vy0tr8cgb")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", uid):
        raise ValueError("InvalidDatasourceUid")
    return values["GRAFANA_TOKEN"], uid


def make_panels(dashboard):
    d = copy.deepcopy(dashboard)
    old = d.get("panels", [])
    ids = set(range(191520, 191524))
    for p in old:
        if p.get("id") in ids and p.get("description") != MARKER:
            raise ValueError("DashboardPanelIdCollision")
    installed = any(p.get("description") == MARKER for p in old)
    remaining = [p for p in old if p.get("description") != MARKER]
    if not installed:
        def shift(rows):
            for p in rows:
                if "gridPos" in p:
                    p["gridPos"]["y"] += 12
                shift(p.get("panels", []))
        shift(remaining)
    new = []
    specs = [("Периметр — состояние HA", "severity", "number"), ("Периметр — ведущий", "leader", "string"), ("Периметр — RFID-поток", "business", "string")]
    for i, (title, field, kind) in enumerate(specs):
        mappings = []
        if field == "severity":
            mappings = [{"type": "value", "options": {"0": {"text": "РАБОТАЕТ", "color": "green"}, "1": {"text": "ВНИМАНИЕ", "color": "yellow"}, "2": {"text": "ОТКАЗ / НЕТ ДАННЫХ", "color": "red"}}}]
        if field == "business":
            mappings = [{"type": "value", "options": {"Без предупреждений": {"text": "Без предупреждений", "color": "green"}, "RFID не готов": {"text": "RFID не готов", "color": "red"}}}]
        p = {"id": 191520+i, "type": "stat", "title": title, "description": MARKER,
             "datasource": DS, "gridPos": {"x": i*8, "y": 0, "w": 8, "h": 4},
             "fieldConfig": {"defaults": {"noValue": "НЕТ ДАННЫХ", "color": {"mode": "thresholds"}, "mappings": mappings,
                  "thresholds": {"mode": "absolute", "steps": [{"value": None, "color": "yellow"}, {"value": 0, "color": "green"}, {"value": 1, "color": "yellow"}, {"value": 2, "color": "red"}]}}, "overrides": []},
             "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": "background", "textMode": "value"},
             "targets": [target("/summary", [(field, field, kind)])]}
        new.append(p)
    new.append({"id": 191523, "type": "table", "title": "Периметр — физика и резервы", "description": MARKER,
         "datasource": DS, "gridPos": {"x": 0, "y": 4, "w": 24, "h": 8}, "options": {"showHeader": True},
         "targets": [target("/nodes", [("label", "Узел", "string"), ("role", "Роль", "string"), ("detail", "Состояние", "string"), ("epoch", "Epoch", "number"), ("http", "HTTP", "number"), ("faulted", "Ремонт", "number"), ("release", "Версия", "string")])]})
    d["panels"] = new + remaining
    d["refresh"] = "5s"
    return d


def target(path, columns):
    return {"refId": "A", "datasource": DS, "type": "json", "source": "url", "parser": "backend", "format": "table",
            "root_selector": "", "url": WALLBOARD_BASE + path, "url_options": {"method": "GET"},
            "columns": [{"selector": a, "text": b, "type": c} for a, b, c in columns]}


def command(argv):
    result = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=25)
    if result.returncode:
        raise RuntimeError("NativeCommandFailed")
    return result.stdout.decode().strip()


def install():
    if os.geteuid() != 0 or sys.platform != "linux":
        raise ValueError("RunOnUb22WithSudo")
    token, zuid = read_env()
    def api(path, body=None, method=None):
        return http_json("http://127.0.0.1:3000" + path, token, body, method)[1]
    def rpc(method, params):
        if method != "host.get":
            raise ValueError("ZabbixProxyReadOnly")
        value = api("/api/datasources/uid/" + zuid + "/resources/zabbix-api", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in value or "result" not in value:
            raise RuntimeError("ZabbixApiRejected_" + method.replace(".", "_"))
        return value["result"]
    dash = api("/api/dashboards/uid/mositlab-director-wallboard")
    if not dash.get("meta", {}).get("canSave"):
        raise ValueError("ExistingDashboardNotWritable")
    ds = api("/api/datasources/uid/wallboard-api")
    if ds.get("type") != DS["type"] or "http://127.0.0.1:19150" not in ds.get("jsonData", {}).get("allowedHosts", []):
        raise ValueError("ExpectedExistingInfinityDatasourceRequired")
    wallboard_source = Path("/opt/mositlab-wallboard/wallboard.py")
    if not wallboard_source.is_file():
        raise ValueError("ExistingWallboardSourceMissing")
    import ast
    tree = ast.parse(wallboard_source.read_text())
    if not any(isinstance(n, ast.ImportFrom) and n.module == "http.server" and any(a.name in ("HTTPServer", "ThreadingHTTPServer") for a in n.names) for n in ast.walk(tree)):
        raise ValueError("WallboardHttpServerAdapterUnsupported")
    dropin = Path("/etc/systemd/system/mositlab-wallboard.service.d/90-perimeter-ha.conf")
    if dropin.is_symlink() or (dropin.exists() and MARKER not in dropin.read_text()):
        raise ValueError("ExistingWallboardDropinCollision")
    exec_value = command(["systemctl", "show", "mositlab-wallboard.service", "-p", "ExecStart", "--value"])
    match = re.search(r"argv\[\]=([^;]+)", exec_value)
    expected = ["/usr/bin/python3", str(wallboard_source)]
    ours = ["/usr/bin/python3", "-B", str(SCRIPT), "--wallboard-proxy", str(wallboard_source)]
    if not match or shlex.split(match.group(1).strip()) not in (expected, ours):
        raise ValueError("ExistingWallboardLauncherUnexpected")
    baseline_projects = http_json("http://127.0.0.1:19150/projects")[1]
    if not isinstance(baseline_projects, list):
        raise ValueError("ExistingWallboardProjectsContractUnexpected")
    hosts = rpc("host.get", {"filter": {"host": ["DESKTOP-OFF5KSM"]}, "output": ["hostid", "host", "status", "proxy_hostid"]})
    if len(hosts) != 1 or hosts[0]["hostid"] != "10539" or hosts[0]["status"] != "0" or hosts[0].get("proxy_hostid", "0") != "0":
        raise ValueError("ExpectedExistingZabbixServerHostRequired")
    host = hosts[0]
    updated = make_panels(dash["dashboard"])
    incoming_token = Path("/home/mkm/.perimeter-ha/observer.token")
    credential = Path("/etc/perimeter-ha-monitor.token")
    if incoming_token.is_symlink() or credential.is_symlink():
        raise ValueError("InvalidObserverToken")
    if incoming_token.exists():
        value = incoming_token.read_text(encoding="utf-8-sig").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("InvalidObserverToken")
        fd = os.open(str(credential), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(value)
    if not observer_token():
        raise ValueError("TransferObserverTokenFromPhysicalFirst")
    # Bind precheck refuses to take another application's listening port.
    unit_path = Path("/etc/systemd/system/" + UNIT)
    if SCRIPT.is_symlink() or unit_path.is_symlink() or (SCRIPT.exists() and MARKER not in SCRIPT.read_text()):
        raise ValueError("ExistingObserverFileCollision")
    if unit_path.exists() and MARKER not in unit_path.read_text():
        raise ValueError("ExistingUnitCollision")
    if not unit_path.exists():
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 19152))
    backup = Path("/var/lib/perimeter-ha-monitor/backups") / uuid.uuid4().hex
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup.parent, 0o700)
    def save(name, value):
        (backup / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))
        os.chmod(backup / name, 0o600)
    save("dashboard.json", dash)
    save("datasource.json", ds)
    if SCRIPT.exists():
        (backup / "previous-monitor.py").write_bytes(SCRIPT.read_bytes())
    if unit_path.exists():
        (backup / "previous-unit.service").write_bytes(unit_path.read_bytes())
    previous_dropin = dropin.read_text() if dropin.exists() else None
    if previous_dropin is not None:
        (backup / "previous-wallboard-dropin.conf").write_text(previous_dropin)
    (backup / "wallboard-source-sha256.txt").write_text(hashlib.sha256(wallboard_source.read_bytes()).hexdigest())
    print("MONITORING_PRECHECK_OK backup=" + str(backup), flush=True)
    SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(SCRIPT.parent, 0o755)
    source = Path(__file__).read_bytes()
    SCRIPT.write_bytes(source)
    os.chmod(SCRIPT, 0o644)
    unit_path.write_text("# " + MARKER + "\n[Unit]\nDescription=Perimeter HA external observer\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=simple\nUser=nobody\nLoadCredential=ha-token:/etc/perimeter-ha-monitor.token\nExecStart=/usr/bin/python3 -B " + str(SCRIPT) + " --serve\nRestart=always\nRestartSec=3\nNoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\nRestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n[Install]\nWantedBy=multi-user.target\n")
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "enable", UNIT])
    command(["systemctl", "restart", UNIT])
    deadline = time.monotonic()+18
    status = None
    while time.monotonic() < deadline:
        try:
            status = http_json(BASE + "/status")[1]
            if all(n["role"] != "ЗАПУСК" for n in status["nodes"].values()):
                break
        except Exception:
            pass
        time.sleep(1)
    if status is None or any(n["role"] == "ЗАПУСК" for n in status["nodes"].values()):
        raise RuntimeError("ObserverInitialPollNotConfirmed")
    print("HA_OBSERVER_SAMPLE " + json.dumps(status, ensure_ascii=False), flush=True)
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text("# " + MARKER + "\n[Service]\nExecStart=\nExecStart=/usr/bin/python3 -B " + str(SCRIPT) + " --wallboard-proxy " + str(wallboard_source) + "\n")
    try:
        command(["systemctl", "daemon-reload"])
        command(["systemctl", "restart", "mositlab-wallboard.service"])
        deadline = time.monotonic()+18
        while True:
            try:
                projects = http_json("http://127.0.0.1:19150/projects")[1]
                api_summary = http_json(WALLBOARD_BASE + "/summary")[1]
                if not isinstance(projects, list) or not isinstance(api_summary, list) or len(api_summary) != 1:
                    raise ValueError("WallboardAdapterContractFailed")
                if {p.get("project") for p in projects} != {p.get("project") for p in baseline_projects}:
                    raise ValueError("ExistingProjectsChanged")
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("WallboardAdapterNotConfirmed")
                time.sleep(1)
    except Exception:
        if previous_dropin is None:
            dropin.unlink(missing_ok=True)
        else:
            dropin.write_text(previous_dropin)
        command(["systemctl", "daemon-reload"])
        command(["systemctl", "restart", "mositlab-wallboard.service"])
        print("ORIGINAL_WALLBOARD_LAUNCHER_RESTORED", flush=True)
        raise
    print("EXISTING_WALLBOARD_AND_HA_ROUTE_CONFIRMED", flush=True)
    # Zabbix configuration requires a direct local API session in finish_monitoring.py.
    # Original version + overwrite=false lets Grafana reject concurrent editing.
    body = {"dashboard": updated, "overwrite": False, "message": MARKER}
    for field in ("folderUid", "folderId"):
        if field in dash.get("meta", {}):
            body[field] = dash["meta"][field]
            break
    api("/api/dashboards/db", body)
    saved = api("/api/dashboards/uid/mositlab-director-wallboard")
    if sum(p.get("description") == MARKER for p in saved["dashboard"]["panels"]) != 4:
        raise RuntimeError("DashboardSaveNotConfirmed")
    q = copy.deepcopy(saved["dashboard"]["panels"][0]["targets"][0])
    q.update(intervalMs=5000, maxDataPoints=1)
    now = int(time.time()*1000)
    frames = api("/api/ds/query", {"from": str(now-60000), "to": str(now), "queries": [q]})
    result = frames.get("results", {}).get("A", {})
    if result.get("error") or not any(any(values for values in frame.get("data", {}).get("values", [])) for frame in result.get("frames", [])):
        raise RuntimeError("GrafanaBackendQueryNotConfirmed")
    print("GRAFANA_HA_BACKEND_DATA_CONFIRMED", flush=True)
    if command(["systemctl", "is-enabled", UNIT]) != "enabled" or command(["systemctl", "is-active", UNIT]) != "active":
        raise RuntimeError("ObserverAutostartNotConfirmed")
    print("OBSERVER_AND_GRAFANA_INSTALLED_AUTOSTART_OK", flush=True)
    print("ZABBIX_SETUP_PENDING_RUN_FINISH_MONITORING", flush=True)
    print("CLUSTER_OPERATIONAL_STATE " + json.dumps(http_json(BASE+"/status")[1], ensure_ascii=False), flush=True)
    incoming_token.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--wallboard-proxy")
    args = parser.parse_args()
    try:
        if args.wallboard_proxy:
            proxy_wallboard(args.wallboard_proxy)
        elif args.serve:
            serve()
        elif args.install:
            install()
        else:
            print(json.dumps(collect(), ensure_ascii=False, indent=2))
    except Exception as exc:
        print("MONITORING_ACTION_INCOMPLETE", error_code(exc), flush=True)
        raise SystemExit(1)
