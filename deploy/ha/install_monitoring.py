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
OBSERVER_STATUS = "http://127.0.0.1:19153/status"
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
    allowed_ha_destinations = {"http://"+ip+":18200/status" for ip in NODES.values()}
    allowed_ha_destinations.update("http://" + ip + ":18200/events/recent" for ip in NODES.values())
    allowed_ha_destinations.add(OBSERVER_STATUS)
    if ha_token and (token or url not in allowed_ha_destinations):
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
            result["snapshot_fresh"] = type(age) in (int, float) and 0 <= age <= 10
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
            result["services"] = {}
            for name in ("RfidReader", "RusGuardSync", "Yolo", "Aggregator", "WebDashboard"):
                service = full.get("services", {}).get(name, {})
                dependencies = service.get("detail", {}).get("dependencies", {})
                result["services"][name] = {
                    "ok": service.get("ok") is True,
                    "observed": "ok" in service,
                    "dependencies": {key: value.get("status", "unknown") for key, value in dependencies.items()
                                     if isinstance(value, dict)},
                }
            result["bus"] = {key: value for key, value in full.get("bus_metrics", {}).items()
                             if type(value) in (int, float) and value >= 0}
            result["bus_age"] = full.get("bus_sample_age", 999999)
            result["mirror"] = full.get("metadata_mirror", {"enabled": False})
            result["controller"] = full.get("controller", {})
            result["update"] = full.get("update", {})
            result["repair"] = full.get("repair", {})
            result["recovery_timing"] = full.get("recovery_timing")
            result["correlation"] = full.get("correlation", {})
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
    data = summarize(nodes, business)
    try:
        code, health = http_json("http://" + NODES["comparator"] + ":5051/health/ready", accept_degraded=True)
        data["gateway"] = {"ready": code == 200 and health.get("status") == "ok", "observed": True}
    except Exception:
        data["gateway"] = {"ready": False, "observed": False}
    token = observer_token()
    data["recent"] = {"configured": False, "stale": True, "events": []}
    if token:
        def fetch_recent(pair):
            node, ip = pair
            try:
                code, value = http_json("http://" + ip + ":18200/events/recent", ha_token=token)
                if (code == 200 and value.get("configured") is True and value.get("node") == node
                        and type(value.get("at")) in (int, float) and isinstance(value.get("events"), list)):
                    return value
            except Exception:
                pass
            return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            available = [value for value in pool.map(fetch_recent, NODES.items()) if value]
        if available:
            data["recent"] = max(available, key=lambda value: (value.get("stale") is False,
                                                              value.get("caught_up") is True, value["at"]))
    recent = data["recent"]
    for key in ("in_24h", "out_24h", "warehouse_only_24h", "recheck_24h"):
        data[key] = recent.get("counts", {}).get(key) if not recent.get("stale", True) and recent.get("caught_up") else None
    return data


def freshness(data, now=None):
    value = copy.deepcopy(data)
    if (time.time() if now is None else now) - value["timestamp"] > 25:
        value["stale"] = True
        value.update(severity=2, detail="Данные наблюдателя устарели", business_level=1, business="НЕИЗВЕСТНО")
        for node in value["nodes"].values():
            node.update(severity=2, role="НЕИЗВЕСТНО", detail="Устаревшие данные")
        if "recent" in value:
            value["recent"]["stale"] = True
    return value


MIRROR_STREAMS = ("events", "warehouse", "tasks", "rfid", "video", "skud")


def mirror_ready(mirror):
    return (mirror.get("enabled") is True and not mirror.get("error")
            and type(mirror.get("retention_days")) is int and mirror["retention_days"] >= 93
            and all(isinstance(mirror.get("streams", {}).get(key), dict)
                    and mirror["streams"][key].get("caught_up") is True
                    and type(mirror["streams"][key].get("age_sec")) in (int, float)
                    and 0 <= mirror["streams"][key]["age_sec"] <= 130 for key in MIRROR_STREAMS))


def queue_rows(data):
    output = []
    for node in NODES:
        value = data["nodes"].get(node, {})
        fresh = not data.get("stale") and value.get("reachable") and value.get("snapshot_fresh") and value.get("bus_age", 999999) <= 15
        bus = value.get("bus", {}) if fresh else {}
        mirror = value.get("mirror", {}) if fresh else {}
        streams = mirror.get("streams", {})
        output.append({"node": LABELS[node], "role": value.get("role", "НЕИЗВЕСТНО"),
                       "rfid": bus.get("rfid_pending"), "video": bus.get("video_pending"),
                       "fallback_pending": bus.get("fallback_pending"),
                       "archive_records": bus.get("fallback_records"),
                       "cache_days": mirror.get("retention_days") if mirror.get("enabled") else None,
                       "cache_rows": sum(row.get("records", 0) for row in streams.values()) if streams else None,
                       "cache_age": max((row.get("age_sec", 999999) for row in streams.values()), default=None),
                       "cache_state": "ГОТОВА" if mirror_ready(mirror) else "ОШИБКА" if mirror.get("error") else "ЗАПОЛНЕНИЕ" if mirror.get("enabled") else "НЕТ ДАННЫХ",
                       "version": value.get("release", "НЕИЗВЕСТНО")[:8],
                       "state": "СВЕЖИЕ" if fresh else "НЕТ СВЕЖИХ ДАННЫХ"})
    return output


def recent_rows(data):
    feed = data.get("recent", {})
    output = []
    stale = data.get("stale") or feed.get("stale", True)
    for row in feed.get("events", [])[:12]:
        if not isinstance(row, dict):
            continue
        physical = bool(row.get("RfidReadCount", 0)) and row.get("SessionCloseReason") != "WAREHOUSE_ONLY"
        direction = {"IN": "ВЪЕЗД", "OUT": "ВЫЕЗД"}.get(row.get("FinalDirection"), "НЕ ОПРЕДЕЛЕНО") if physical else "СКЛАД"
        warnings = str(row.get("WarningFlags") or "")
        state = "УСТАРЕЛО" if stale else "КОНФЛИКТ" if "DIRECTION_CONFLICT" in warnings else "ПЕРЕПРОВЕРКА" if row.get("NeedRecheck") else "RFID" if physical else "ТОЛЬКО СКЛАД"
        output.append({"time": row.get("FirstSeen"), "direction": direction,
                       "reel": row.get("SeriesNumber") or str(row.get("SourceTag") or "—")[-16:],
                       "tag": row.get("SourceTag"), "reads": row.get("RfidReadCount"),
                       "video": "●" if row.get("VideoMatched") else "—",
                       "warehouse": row.get("WarehouseDt") or "—", "state": state,
                       "node": feed.get("node", "—"), "event_id": row.get("EventId")})
    return output or [{"time": "—", "direction": "НЕТ ДАННЫХ", "reel": "—", "reads": None,
                       "video": "—", "warehouse": "—", "state": "Копия не настроена или пуста", "event_id": None}]


def diagram_rows(data):
    """Flat Grafana data frame: explicit cards and directed edges, no HTML."""
    rows = []
    nodes = data["nodes"]
    active = [node for node in NODES if nodes.get(node, {}).get("reachable") and nodes[node].get("active")]
    owner = active[0] if len(active) == 1 and nodes[active[0]].get("severity") == 0 and not data.get("stale") else None
    def card(key, label, detail, state, x, y, w=350, h=42, kind="card"):
        rows.append(dict(id=key, kind=kind, label=label, detail=detail, state=state, x=x, y=y, w=w, h=h))
    def edge(key, source, target, enabled=False, control=False):
        rows.append(dict(id=key, kind="edge", source=source, target=target,
                         state="control" if enabled and control else "active" if enabled else "off"))
    def dependency(service, name):
        status = nodes.get(owner, {}).get("services", {}).get(service, {}).get("dependencies", {}).get(name) if owner else None
        return "active" if status == "ok" else "critical" if status == "unavailable" else "warning" if status == "degraded" else "unknown"
    card("source-rfid", "RFID • 4 антенны", "считыватель", dependency("RfidReader", "rfid_reader"), 60, 8, h=38)
    camera_states = [dependency("Yolo", "camera_0"), dependency("Yolo", "camera_1")]
    camera_state = "active" if all(s == "active" for s in camera_states) else "critical" if "critical" in camera_states else "unknown"
    card("source-video", "Камеры 0 + 1", "YOLO • погрузчик / катушка", camera_state, 525, 8, h=38)
    card("source-skud", "RusGuard", "источник СКУД", dependency("RusGuardSync", "source_database"), 990, 8, h=38)
    for index, node in enumerate(NODES):
        value = nodes.get(node, {})
        x = 60 + index * 465
        state = "unknown" if data.get("stale") or "reachable" not in value else "critical" if not value.get("reachable") else "active" if node == owner else "critical" if value.get("faulted") or value.get("active") else "ready" if value.get("prepared") else "warning"
        card(node, LABELS[node] + " • " + ("1" if index == 0 else "2" if index == 1 else "3"),
             value.get("role", "НЕИЗВЕСТНО") + "  |  " + NODES[node] + "  |  " + value.get("release", "—")[:8], state, x, 80, h=48, kind="header")
        for name, label, offset in (("RfidReader", "RFID", 0), ("Yolo", "YOLO", 120), ("RusGuardSync", "СКУД", 240)):
            service = value.get("services", {}).get(name, {})
            observed = value.get("snapshot_fresh") and service.get("observed") and not data.get("stale")
            status = "active" if observed and service.get("ok") and value.get("active") else "critical" if observed and value.get("active") and not service.get("ok") else "off" if state == "ready" else "unknown"
            key = node + "-" + name
            card(key, label, "работает" if status == "active" else "резерв" if status == "off" else "нет данных" if status == "unknown" else "отказ", status, x + offset, 152, w=110)
            edge("input-" + key, {"RfidReader": "source-rfid", "Yolo": "source-video", "RusGuardSync": "source-skud"}[name], key, node == owner and status == "active")
            edge("queue-" + key, key, node + "-queue", node == owner and status == "active")
        local_fresh = value.get("reachable") and value.get("snapshot_fresh") and not data.get("stale")
        bus = value.get("bus", {}) if value.get("bus_age", 999999) <= 15 and local_fresh else {}
        count = bus.get("rfid_pending", 0) + bus.get("video_pending", 0)
        mirror = value.get("mirror", {}) if local_fresh else {}
        queues_known = "rfid_pending" in bus and "video_pending" in bus
        detail = ("RFID " + str(bus["rfid_pending"]) + "  /  YOLO " + str(bus["video_pending"])) if queues_known else "Очереди: нет данных"
        copy_ready = mirror_ready(mirror)
        detail += "  •  " + (str(mirror["retention_days"]) + " дней копии" if copy_ready else "копия: ошибка" if mirror.get("error") else "копия: заполнение" if mirror.get("enabled") else "копия: нет данных")
        card(node + "-queue", "Очереди + локальная копия", detail,
             "critical" if mirror.get("error") else "warning" if (queues_known and count) or (mirror.get("enabled") and not copy_ready) else "active" if queues_known and copy_ready and node == owner else "ready" if copy_ready else "unknown", x, 220, h=48)
        for name, label, y in (("Aggregator", "Сопоставление", 362), ("WebDashboard", "WEB • 5050", 500)):
            service = value.get("services", {}).get(name, {})
            observed = value.get("snapshot_fresh") and service.get("observed") and not data.get("stale")
            status = "active" if observed and service.get("ok") and node == owner else "critical" if observed and value.get("active") and not service.get("ok") else "off" if state == "ready" else "unknown"
            card(node + "-" + name, label, "RFID + видео + СКУД + склад" if name == "Aggregator" else "итоговые проходы и снимки", status, x, y)
    database = dependency("Aggregator", "database")
    card("sql-input", "SQL • исходные данные", "единая база • входная шина", database, 525, 292)
    card("sql-result", "SQL • итоговые события", "идентичность / направление / склад", database, 525, 430)
    mirror = nodes.get(owner, {}).get("mirror", {}) if owner else {}
    wh = mirror.get("streams", {}).get("warehouse", {})
    wh_state = "active" if wh.get("caught_up") and wh.get("age_sec", 999999) <= 130 and not data.get("stale") else "warning" if wh else "unknown"
    card("warehouse", "Склад + 1C", "TAG → IDS → SERIES", wh_state, 60, 292)
    controllers = [value.get("controller", {}) for value in nodes.values()]
    controllers = [value for value in controllers if value.get("valid") is True and value.get("owner") in NODES
                   and 0 <= time.time() - value.get("at", 0) <= 130 and not data.get("stale")]
    control_owner = controllers[0]["owner"] if controllers and len({v["owner"] for v in controllers}) == 1 else None
    lease_detail = LABELS.get(control_owner, "НЕИЗВЕСТНО")
    if owner:
        lease_detail += " • epoch " + str(nodes[owner].get("epoch", "—"))
    timing = nodes.get(control_owner, {}).get("recovery_timing") or {}
    rto = timing.get("readiness_rto_sec")
    if type(rto) in (int, float) and rto >= 0:
        lease_detail += " • RTO " + str(round(rto, 1)) + " с"
    card("lease", "HA • lease / epoch", lease_detail, "control" if control_owner else "unknown", 990, 292)
    correlation = nodes.get(owner, {}).get("correlation", {}) if owner else {}
    mode = correlation.get("mode", "не опубликовано")
    card("windows", "Окна КПП → склад", mode, "warning" if mode == "shadow" else "active" if mode == "active" else "unknown", 60, 430)
    observed_repair = [v for v in nodes.values() if v.get("reachable") and v.get("snapshot_fresh")
                       and type(v.get("repair", {}).get("verification_required")) is bool]
    pending_repairs = sum(v["repair"]["verification_required"] for v in observed_repair)
    trials = sum(bool(v.get("update", {}).get("pending")) for v in observed_repair)
    quarantined = sum(v.get("update", {}).get("quarantined", 0) for v in observed_repair
                      if type(v.get("update", {}).get("quarantined")) is int)
    repair_state = "unknown" if data.get("stale") or len(observed_repair) != 3 else "warning" if pending_repairs or trials or quarantined else "ready"
    repair_detail = "нет свежей телеметрии" if repair_state == "unknown" else (
        "ремонт " + str(pending_repairs) + " • trial " + str(trials) + " • quarantine " + str(quarantined))
    card("repair", "Ремонт + проверка обновлений", repair_detail, repair_state, 990, 430)
    gateway = data.get("gateway", {})
    gateway_state = "active" if gateway.get("ready") and owner and not data.get("stale") else "warning" if gateway.get("observed") else "unknown"
    card("gateway", "WEB • постоянный адрес", "Comparator :5051 • независимый сервис", gateway_state, 525, 572, h=44)
    edge("warehouse-sql", "warehouse", "sql-input", wh_state == "active" and database == "active")
    edge("lease-sql", "lease", "sql-input", bool(control_owner), control=True)
    for node in NODES:
        edge(node + "-raw-sql", node + "-queue", "sql-input", node == owner and database == "active")
        edge(node + "-sql-agg", "sql-input", node + "-Aggregator", node == owner and database == "active")
        edge(node + "-agg-result", node + "-Aggregator", "sql-result", node == owner and database == "active")
        edge(node + "-result-web", "sql-result", node + "-WebDashboard", node == owner and database == "active")
        edge(node + "-web-gateway", node + "-WebDashboard", "gateway", node == owner and gateway_state == "active")
    return rows


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
                value = [{k: v for k, v in data.items() if k not in {"nodes", "recent"}}]
            elif self.path == "/nodes":
                value = list(data["nodes"].values())
            elif self.path == "/diagram":
                value = diagram_rows(data)
            elif self.path == "/recent":
                value = recent_rows(data)
            elif self.path == "/queues":
                value = queue_rows(data)
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
                                value=http_json(OBSERVER_STATUS,ha_token=observer_token())[1]
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
                        if self.path not in ("/perimeter-ha/status", "/perimeter-ha/summary", "/perimeter-ha/nodes",
                                             "/perimeter-ha/diagram", "/perimeter-ha/recent", "/perimeter-ha/queues"):
                            return super().do_GET()
                        route = self.path.removeprefix("/perimeter-ha")
                        try:
                            value = http_json(BASE + route)[1]
                        except Exception:
                            data = summarize([probe_result(n) for n in NODES])
                            data.update(severity=2, detail="Наблюдатель HA недоступен")
                            value = (diagram_rows(data) if route == "/diagram" else recent_rows(data) if route == "/recent"
                                     else queue_rows(data) if route == "/queues" else data if route == "/status"
                                     else list(data["nodes"].values()) if route == "/nodes"
                                     else [{k: v for k, v in data.items() if k not in {"nodes", "recent"}}])
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
