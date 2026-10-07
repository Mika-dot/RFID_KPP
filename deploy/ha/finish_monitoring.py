"""Finish already-installed ub22 observer; diagnose and request guarded passive repair.

Run downloaded copy sudo python3 -B tool --finish-grafana --diagnose --repair-passive
--configure-zabbix. Zabbix login is local/hidden and is never stored. No SQL,
maintenance, promote/demote, cursor/spool/latch or active-worker changes here.
"""
import argparse
import concurrent.futures
import copy
import getpass
import importlib.util
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

MARKER = "Managed by Perimeter HA external observer v1"
AGENTS = {"physical": "172.31.0.188", "perimetr": "172.31.0.134", "comparator": "172.31.0.192"}
SERVICES = ("RfidReader", "RusGuardSync", "Yolo", "Aggregator", "WebDashboard")
SENSITIVE = re.compile(r"password|passwd|pwd|secret|token|authorization|conn_str|connection_string", re.I)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def load_observer():
    path = Path("/usr/local/lib/perimeter-ha-monitor/monitor.py")
    if not path.is_file() or MARKER not in path.read_text():
        raise RuntimeError("ExpectedInstalledObserverRequired")
    spec = importlib.util.spec_from_file_location("installed_ha_observer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scrub(value, secrets=()):
    if isinstance(value, dict):
        return {str(k): ("REDACTED" if SENSITIVE.search(str(k)) else scrub(v, secrets)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        value = value.replace(secret, "REDACTED")
    value = re.sub(r'(?i)(password|passwd|pwd|token|secret|authorization|uid|user id)\s*[=:]\s*(?:\{(?:\}\}|[^}])*\}|"[^"]*"|\x27[^\x27]*\x27|[^;\s,]+)', r'\1=REDACTED', value)
    value = re.sub(r"(?i)(rtsp|https?)://[^\s/@]+@", r"\1://REDACTED@", value)
    return value[:4000]


def ha_request(node, token, endpoint, payload=None):
    # Auth credential must never follow a redirect or a caller-supplied URL.
    if node not in AGENTS or endpoint not in ("/status", "/diagnostics", "/repair"):
        raise ValueError("ForbiddenAgentRequest")
    if payload is not None and (endpoint != "/repair" or payload != {"action": "restart_service", "service": "all"}):
        raise ValueError("ForbiddenAgentAction")
    if endpoint == "/repair" and payload is None:
        raise ValueError("RepairRequiresTypedPayload")
    url = "http://" + AGENTS[node] + ":18200" + endpoint
    req = Request(url, data=None if payload is None else json.dumps(payload).encode(), headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        response = opener.open(req, timeout=100 if payload is not None else 6)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read(512*1024+1)
        if len(raw) > 512*1024:
            raise ValueError("AgentResponseTooLarge")
        return response.code, json.loads(raw)


def valid_status(value, node):
    return (isinstance(value, dict) and value.get("node") == node and value.get("fencing_protocol") == 2
            and type(value.get("sample_age")) in (float, int) and 0 <= value["sample_age"] < 10
            and all(type(value.get(k)) is bool for k in ("active", "healthy", "prepared", "faulted", "operator_maintenance"))
            and type(value.get("epoch")) is int and isinstance(value.get("release_sha"), str)
            and bool(re.fullmatch(r"[0-9a-f]{40}", value["release_sha"])))


def snapshot(token):
    def one(node):
        try:
            code, value = ha_request(node, token, "/status")
            if code != 200 or not valid_status(value, node):
                raise ValueError("InvalidAgentSnapshot")
            return node, value
        except Exception as exc:
            return node, {"error": type(exc).__name__}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        return dict(pool.map(one, AGENTS))


def diagnostic_report(status, diag, token):
    safe = {k: status.get(k) for k in ("node", "active", "healthy", "prepared", "faulted", "epoch", "sample_age", "release_sha", "main_sha", "operator_maintenance", "fencing_protocol", "preflight", "resources")}
    safe["workers"] = diag.get("workers", {})
    safe["services"] = {}
    for name, service in status.get("services", {}).items():
        if name not in SERVICES:
            continue
        detail = service.get("detail", {})
        safe["services"][name] = {"ok": service.get("ok"), "error": service.get("error"),
            "uptime_seconds": detail.get("uptime_seconds"), "dependencies": detail.get("dependencies", {}),
            "warnings": detail.get("warnings", {}), "metrics": detail.get("metrics", {})}
    # Read-only logs are already redacted by agents, then scrubbed again locally.
    names = ("RfidReader", "Aggregator", "WebDashboard") if status.get("active") else ("RfidReader",)
    safe["logs"] = {name: str(diag.get("logs", {}).get(name, ""))[-2800:] for name in names}
    return scrub(safe, (token,))


def diagnose(token):
    def one(node):
        try:
            code, diag = ha_request(node, token, "/diagnostics")
            status = diag.get("status", {})
            if code != 200 or not valid_status(status, node):
                raise ValueError("InvalidAgentDiagnostics")
            return node, diagnostic_report(status, diag, token)
        except Exception as exc:
            return node, {"error": type(exc).__name__}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        result = dict(pool.map(one, AGENTS))
    print("HA_DIAGNOSTICS_READ_ONLY " + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def brief(statuses):
    return {node: {k: s.get(k) for k in ("active", "healthy", "prepared", "faulted", "epoch", "release_sha", "error")} for node, s in statuses.items()}


def cluster_ready(statuses):
    if any(not valid_status(s, n) or s["operator_maintenance"] for n, s in statuses.items()) or set(statuses) != set(AGENTS):
        return False
    active = [s for s in statuses.values() if s["active"]]
    return (len(active) == 1 and active[0]["healthy"] and active[0]["prepared"] and not active[0]["faulted"]
            and all(s["prepared"] and not s["faulted"] and s["epoch"] == active[0]["epoch"] for s in statuses.values()))


def eligible_passive(node, status):
    return (valid_status(status, node) and not status["active"] and status["faulted"] and status["prepared"]
            and not status["operator_maintenance"] and status.get("preflight", {}).get("ok") is True
            and not status.get("resources", {}).get("restart_required"))


def repair_passive(token):
    # At most one attempt per node, server's own SQL ownership/rate guards apply.
    for node in ("perimetr", "comparator", "physical"):
        code, current = ha_request(node, token, "/status")
        if code != 200 or not eligible_passive(node, current):
            print("PASSIVE_REPAIR_SKIPPED " + node, flush=True)
            continue
        code, diag = ha_request(node, token, "/diagnostics")
        verified = diag.get("status", {})
        if code != 200 or not eligible_passive(node, verified) or diag.get("workers") != {} or verified["epoch"] != current["epoch"] or verified["release_sha"] != current["release_sha"]:
            print("PASSIVE_REPAIR_STATE_CHANGED " + node, flush=True)
            continue
        code, result = ha_request(node, token, "/repair", {"action": "restart_service", "service": "all"})
        print("PASSIVE_REPAIR_REQUEST_RESULT " + json.dumps({"node": node, "http": code, "result": scrub(result, (token,))}, ensure_ascii=False), flush=True)
    print("WAITING_FOR_GUARDIAN_INDEPENDENT_RECOVERY", flush=True)
    deadline = time.monotonic()+105
    good_since = None
    while time.monotonic() < deadline:
        statuses = snapshot(token)
        print("HA_RECOVERY_STATE " + json.dumps(brief(statuses), ensure_ascii=False), flush=True)
        if cluster_ready(statuses):
            if good_since is None:
                good_since = time.monotonic()
            if time.monotonic()-good_since >= 25:
                print("HA_ONE_HEALTHY_LEADER_TWO_READY_RESERVES_CONFIRMED", flush=True)
                return True
        else:
            good_since = None
        time.sleep(5)
    print("HA_RECOVERY_NOT_CONFIRMED_ROOT_CAUSE_REQUIRED", flush=True)
    diagnose(token)
    return False


def finish_grafana(m):
    token, _ = m.read_env()
    def api(path, body=None):
        return m.http_json("http://127.0.0.1:3000"+path, token=token, body=body)[1]
    if m.command(["systemctl", "is-active", m.UNIT]) != "active" or m.command(["systemctl", "is-enabled", m.UNIT]) != "enabled":
        raise RuntimeError("IndependentObserverNotRunningEnabled")
    summary = m.http_json(m.WALLBOARD_BASE+"/summary")[1]
    if not isinstance(summary, list) or len(summary) != 1 or summary[0].get("severity") not in (0, 1, 2):
        raise ValueError("LiveObserverDataRequired")
    old = api("/api/dashboards/uid/mositlab-director-wallboard")
    if not old.get("meta", {}).get("canSave"):
        raise ValueError("ExistingDashboardNotWritable")
    new = m.make_panels(old["dashboard"])
    # The installed observer can still generate the older numeric-only Stat
    # defaults. Include string fields so leader/business values are rendered.
    for panel in new["panels"]:
        if panel.get("description") == MARKER and panel.get("type") == "stat":
            panel["options"]["reduceOptions"]["fields"] = "/.*/"
    backup = Path("/var/lib/perimeter-ha-monitor/backups") / ("grafana-"+uuid.uuid4().hex)
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup, 0o700)
    (backup/"dashboard.json").write_text(json.dumps(old, ensure_ascii=False))
    os.chmod(backup/"dashboard.json", 0o600)
    if new != old["dashboard"]:
        body = {"dashboard": new, "overwrite": False, "message": MARKER}
        for field in ("folderUid", "folderId"):
            if field in old.get("meta", {}):
                body[field] = old["meta"][field]
                break
        api("/api/dashboards/db", body)
    saved = api("/api/dashboards/uid/mositlab-director-wallboard")
    if sum(p.get("description") == MARKER for p in saved["dashboard"]["panels"]) != 4:
        raise RuntimeError("DashboardSaveNotConfirmed")
    now = int(time.time()*1000)
    # Check all4 live queries, not just a saved panel definition.
    for panel in saved["dashboard"]["panels"]:
        if panel.get("description") != MARKER:
            continue
        query = copy.deepcopy(panel["targets"][0])
        query.update(intervalMs=5000, maxDataPoints=5)
        result = api("/api/ds/query", {"from": str(now-60000), "to": str(now), "queries": [query]}).get("results", {}).get("A", {})
        if result.get("error") or not any(any(v for v in f.get("data", {}).get("values", [])) for f in result.get("frames", [])):
            raise RuntimeError("GrafanaLiveQueryNotConfirmed")
    print("GRAFANA_FOUR_HA_PANELS_LIVE_AUTOSTART_OBSERVER_OK", flush=True)
    print("GRAFANA_URL http://172.31.0.97:3000/d/mositlab-director-wallboard", flush=True)


def direct_zabbix_session(m, username, password):
    url = "http://127.0.0.1/api_jsonrpc.php"
    def call(method, params, auth=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        if auth:
            body["auth"] = auth
        value = m.http_json(url, body=body)[1]
        if "error" in value or "result" not in value:
            code = value.get("error", {}).get("code")
            label = "ZabbixApiRejected_"+method.replace(".", "_")
            print(label + (" code="+str(code) if type(code) is int else ""), flush=True)
            raise RuntimeError(label)
        return value["result"]
    version = call("apiinfo.version", {})
    if not isinstance(version, str) or not version.startswith("6."):
        raise ValueError("Zabbix6ApiRequired")
    session = call("user.login", {"username": username, "password": password})
    if not isinstance(session, str) or not re.fullmatch(r"[a-f0-9]{32,64}", session):
        raise ValueError("InvalidZabbixLoginResponse")
    def rpc(method, params):
        if method not in {"host.get", "item.get", "item.create", "item.update", "trigger.get", "trigger.create", "trigger.update", "user.logout"}:
            raise ValueError("ForbiddenZabbixMethod")
        return call(method, params, session)
    return rpc


def configure_zabbix(m, rpc):
    hosts = rpc("host.get", {"filter": {"host": ["DESKTOP-OFF5KSM"]}, "output": ["hostid", "host", "status", "proxy_hostid"]})
    if len(hosts) != 1 or hosts[0].get("hostid") != "10539" or hosts[0].get("status") != "0" or hosts[0].get("proxy_hostid", "0") != "0":
        raise ValueError("ExpectedExistingZabbixHostRequired")
    host = hosts[0]
    backup = Path("/var/lib/perimeter-ha-monitor/backups") / ("zabbix-"+uuid.uuid4().hex)
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup, 0o700)
    def save(name, value):
        (backup/name).write_text(json.dumps(value, ensure_ascii=False))
        os.chmod(backup/name, 0o600)
    def item(spec):
        prior = rpc("item.get", {"hostids": ["10539"], "filter": {"key_": [spec["key_"]]}, "output": "extend", "selectPreprocessing": "extend"})
        if prior:
            if len(prior) != 1 or prior[0].get("description") != MARKER:
                raise ValueError("ExistingItemCollision")
            save("item-"+prior[0]["itemid"]+".json", prior)
            rpc("item.update", dict({k: v for k, v in spec.items() if k != "hostid"}, itemid=prior[0]["itemid"]))
            return prior[0]["itemid"]
        return rpc("item.create", spec)["itemids"][0]
    master = item({"hostid": "10539", "name": "Периметр HA: полный статус кластера", "key_": m.KEY, "type": 19, "value_type": 4,
                   "url": m.BASE+"/status", "delay": "5s", "timeout": "5s", "status_codes": "200", "retrieve_mode": 0,
                   "history": "1d", "status": 0, "description": MARKER})
    keys = {"severity": "$.severity", "active_count": "$.active_count", "ready_reserves": "$.ready_reserves", "business_level": "$.business_level"}
    for node in AGENTS:
        for field in ("reachable", "active", "healthy", "prepared", "faulted", "epoch"):
            keys[node+"."+field] = "$.nodes."+node+"."+field
    for key, path in keys.items():
        item({"hostid": "10539", "name": "Периметр HA: "+key, "key_": "perimeter.ha."+key, "type": 18, "master_itemid": master,
              "value_type": 3, "delay": "0", "history": "30d", "trends": "365d", "status": 0, "description": MARKER,
              "preprocessing": [{"type": 12, "params": path, "error_handler": 0, "error_handler_params": ""}]})
    for title, expr, priority in [("Периметр HA: наблюдатель недоступен", "nodata(/"+host["host"]+"/"+m.KEY+",45s)=1", 4),
                ("Периметр HA: нет готового единственного ведущего", "last(/"+host["host"]+"/perimeter.ha.severity)=2", 4),
                ("Периметр HA: резерв или RFID-поток требует внимания", "last(/"+host["host"]+"/perimeter.ha.severity)=1", 2)]:
        prior = rpc("trigger.get", {"hostids": ["10539"], "filter": {"description": [title]}, "output": "extend"})
        spec = {"description": title, "expression": expr, "priority": priority, "status": 0, "comments": MARKER,
                "tags": [{"tag": "component", "value": "perimeter-ha"}]}
        if prior:
            if len(prior) != 1 or prior[0].get("comments") != MARKER:
                raise ValueError("ExistingTriggerCollision")
            save("trigger-"+prior[0]["triggerid"]+".json", prior)
            rpc("trigger.update", dict(spec, triggerid=prior[0]["triggerid"]))
        else:
            rpc("trigger.create", spec)
    print("ZABBIX_DIRECT_HA_ITEMS_AND_TRIGGERS_CONFIGURED", flush=True)
    deadline = time.monotonic()+65
    while time.monotonic() < deadline:
        rows = rpc("item.get", {"itemids": [master], "output": ["state", "lastclock"]})
        if rows and rows[0].get("state") == "0" and int(rows[0].get("lastclock", "0")) > time.time()-30:
            print("ZABBIX_HA_LIVE_HISTORY_CONFIRMED", flush=True)
            return
        time.sleep(3)
    raise RuntimeError("ZabbixHistoryNotConfirmed")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--finish-grafana", action="store_true")
    p.add_argument("--diagnose", action="store_true")
    p.add_argument("--repair-passive", action="store_true")
    p.add_argument("--configure-zabbix", action="store_true")
    args = p.parse_args()
    if os.geteuid() != 0:
        raise ValueError("RunOnUb22WithSudo")
    m = load_observer()
    incomplete = False
    if args.finish_grafana:
        try:
            finish_grafana(m)
        except Exception as exc:
            incomplete = True
            print("GRAFANA_FINISH_ACTION_INCOMPLETE", safe_error(exc), flush=True)
    token = m.observer_token()
    if not token:
        raise ValueError("ExistingObserverTokenRequired")
    if args.diagnose:
        diagnose(token)
    if args.repair_passive:
        try:
            repair_passive(token)
        except Exception as exc:
            incomplete = True
            print("PASSIVE_REPAIR_ACTION_INCOMPLETE", type(exc).__name__, flush=True)
            diagnose(token)
    if args.configure_zabbix:
        print("Zabbix API login is local, hidden, not stored; Grafana and observer are already running.", flush=True)
        username = input("Zabbix login [Admin] (Enter default, '-' skip): ").strip() or "Admin"
        if username == "-":
            print("ZABBIX_SETUP_PENDING_DIRECT_LOGIN", flush=True)
            incomplete = True
        else:
            rpc = None
            password = None
            try:
                password = getpass.getpass("Zabbix password (hidden): ")
                rpc = direct_zabbix_session(m, username, password)
                password = None
                configure_zabbix(m, rpc)
            except Exception as exc:
                incomplete = True
                print("ZABBIX_FINISH_ACTION_INCOMPLETE", safe_error(exc), flush=True)
            finally:
                password = None
                if rpc:
                    try:
                        rpc("user.logout", [])
                    except Exception:
                        pass
    print("MONITORING_FINISH_HAS_PENDING_ACTIONS" if incomplete else "MONITORING_FINISH_ACTIONS_COMPLETED", flush=True)
    final = snapshot(token)
    print("FINAL_CLUSTER_STATE " + json.dumps(brief(final), ensure_ascii=False), flush=True)
    ready = cluster_ready(final)
    print("FINAL_HA_READY" if ready else "FINAL_HA_NOT_READY", flush=True)
    return 1 if incomplete or not ready else 0


def safe_error(exc):
    known = {"ExpectedInstalledObserverRequired", "IndependentObserverNotRunningEnabled", "LiveObserverDataRequired", "ExistingDashboardNotWritable",
             "DashboardSaveNotConfirmed", "GrafanaLiveQueryNotConfirmed", "Zabbix6ApiRequired", "InvalidZabbixLoginResponse", "ForbiddenZabbixMethod",
             "ExpectedExistingZabbixHostRequired", "ExistingItemCollision", "ExistingTriggerCollision", "ZabbixHistoryNotConfirmed", "RunOnUb22WithSudo", "ExistingObserverTokenRequired"}
    reason = str(exc)
    return reason if reason in known else "HTTP_"+str(exc.code) if isinstance(exc, HTTPError) else type(exc).__name__


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("MONITORING_FINISH_ACTION_INCOMPLETE", safe_error(exc), flush=True)
        raise SystemExit(1)
