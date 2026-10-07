"""Read existing ub22 monitoring metadata; never print or transmit stored secrets.

Run once on ub22 with sudo python3 -B inspect_monitoring.py. No writes or restart.
The Grafana credential stays on ub22 and is used only against its local API.
"""
import concurrent.futures
import json
import re
import shlex
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def env_values(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        if key.strip() not in ("GRAFANA_TOKEN", "ZABBIX_UID"):
            continue
        parts = shlex.split(value, comments=True)
        if len(parts) == 1:
            values[key.strip()] = parts[0]
    return values


def fetch(url, token=None, body=None):
    # Never send the private Grafana token to an external URL or redirected host.
    if token and urlsplit(url).netloc != "127.0.0.1:3000":
        raise ValueError("CredentialDestinationRefused")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers)
    opener = build_opener(ProxyHandler({}), NoRedirects())
    with opener.open(req, timeout=5) as response:
        return json.loads(response.read(2 * 1024 * 1024))


def safe_error(exc):
    return "HTTP_" + str(exc.code) if isinstance(exc, HTTPError) else type(exc).__name__


def endpoint(url):
    p = urlsplit(url)
    return p.scheme + "://" + (p.hostname or "") + (":" + str(p.port) if p.port else "") + p.path


def panels(rows):
    for p in rows:
        yield p
        yield from panels(p.get("panels", []))


def inspect():
    env = env_values("/etc/mositlab-wallboard.env")
    token = env.get("GRAFANA_TOKEN")
    if not token:
        raise ValueError("ExistingGrafanaTokenMissing")
    report = {}
    def api(path, body=None):
        return fetch("http://127.0.0.1:3000" + path, token, body)
    d = api("/api/dashboards/uid/mositlab-director-wallboard")
    dashboard = d["dashboard"]
    report["dashboard"] = {k: dashboard.get(k) for k in ("uid", "title", "version")}
    report["can_save"] = d.get("meta", {}).get("canSave")
    report["panels"] = []
    for p in panels(dashboard.get("panels", [])):
        targets = []
        for t in p.get("targets", []):
            targets.append({k: t[k] for k in ("type", "source", "parser", "format", "root_selector", "columns", "refId") if k in t})
            if isinstance(t.get("url"), str):
                targets[-1]["endpoint"] = endpoint(t["url"])
        report["panels"].append({"id": p.get("id"), "title": p.get("title"), "type": p.get("type"), "datasource": p.get("datasource"), "targets": targets})
    report["datasources"] = []
    for uid in ("wallboard-api", env.get("ZABBIX_UID", "bfvr5vy0tr8cgb")):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", uid):
            raise ValueError("InvalidDatasourceUid")
        try:
            ds = api("/api/datasources/uid/" + uid)
            report["datasources"].append({k: ds.get(k) for k in ("uid", "name", "type")})
            report["datasources"][-1]["endpoint"] = endpoint(ds.get("url", ""))
            if uid == "wallboard-api":
                report["datasources"][-1]["allowed_hosts"] = [endpoint(x) for x in ds.get("jsonData", {}).get("allowedHosts", [])]
        except Exception as exc:
            report["datasources"].append({"uid": uid, "error": safe_error(exc)})
    uid = env.get("ZABBIX_UID", "bfvr5vy0tr8cgb")
    proxy = "/api/datasources/uid/" + uid + "/resources/zabbix-api"
    try:
        answer = api(proxy, {"jsonrpc": "2.0", "id": 1, "method": "host.get", "params": {"output": ["hostid", "host", "name"], "selectInterfaces": ["ip", "dns", "useip"]}})
        if "error" in answer:
            report["zabbix_error"] = "ReadOnlyHostQueryRejected"
        else:
            report["hosts"] = [h for h in answer.get("result", []) if any(s in json.dumps(h).lower() for s in ("perimet", "comparator", "off5ksm", "172.31.0.188"))]
    except Exception as exc:
        report["zabbix_error"] = safe_error(exc)
    urls = {"physical": "http://172.31.0.188:18200/health/ready", "perimetr": "http://perimetr:18200/health/ready", "comparator": "http://Comparator:18200/health/ready"}
    def probe(pair):
        node, url = pair
        try:
            value = fetch(url)
            return node, {k: value.get(k) for k in ("node", "active", "healthy", "prepared", "faulted", "epoch")}
        except Exception as exc:
            return node, {"error": safe_error(exc)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        report["ha_from_ub22"] = dict(executor.map(probe, urls.items()))
    return report


if __name__ == "__main__":
    try:
        print(json.dumps(inspect(), ensure_ascii=False, indent=2))
        print("MONITORING_INVENTORY_COMPLETE_READ_ONLY")
    except Exception as exc:
        print("MONITORING_INVENTORY_STOPPED", safe_error(exc))
        raise SystemExit(1)
