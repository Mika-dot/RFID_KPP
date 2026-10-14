#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only, bounded audit of the existing MOSITLAB Grafana wallboard on ub22.

Does not alter Grafana, SQL, HA, dashboards, playlists, or systemd services.
The only POST is Grafana /api/ds/query (read-only datasource queries).
No raw business records, credentials, token, tag values or query results printed.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

GRAFANA = "http://127.0.0.1:3000"
WALLBOARD = "http://127.0.0.1:19150"
DASHBOARDS = ("mositlab-director-wallboard", "mositlab-perimeter-visual")
PLAYLIST = "mositlab-perimeter-wallboard"
PLAYLIST_ROUTE = "/apis/playlist.grafana.app/v1/namespaces/default/playlists/" + PLAYLIST
PANEL_PLUGIN = "volkovlabs-echarts-panel"
LOCAL_ROUTES = (
    "/projects", "/summary", "/attention",
    "/perimeter-ha/summary", "/perimeter-ha/nodes",
    "/perimeter-ha/diagram", "/perimeter-ha/queues",
    "/perimeter-ha/recent", "/perimeter-ha/status",
)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

OPENER = build_opener(ProxyHandler({}), NoRedirect())

def token_from_env(path: Path) -> str:
    values = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "GRAFANA_TOKEN":
            continue
        parsed = shlex.split(value, comments=False, posix=True)
        if len(parsed) != 1 or not parsed[0]:
            raise ValueError("InvalidGrafanaTokenFile")
        values[key.strip()] = parsed[0]
    if not values.get("GRAFANA_TOKEN"):
        raise ValueError("GrafanaTokenNotFound")
    return values["GRAFANA_TOKEN"]

def request_json(url: str, token: str | None = None, body=None, timeout=10):
    if not (url.startswith(GRAFANA + "/") or url.startswith(WALLBOARD + "/")):
        raise ValueError("UnapprovedLocalDestination")
    if token and not url.startswith(GRAFANA + "/"):
        raise ValueError("TokenDestinationRefused")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, headers=headers, data=data)
    with OPENER.open(req, timeout=timeout) as response:
        payload = response.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("OversizedResponse")
        return json.loads(payload) if payload else {}

def error_kind(exc):
    if isinstance(exc, HTTPError):
        return "HTTP_" + str(exc.code)
    if isinstance(exc, URLError):
        return "NETWORK_" + type(exc.reason).__name__
    return type(exc).__name__

def safe_label(value, size=110):
    if not isinstance(value, (str, int, float, bool)):
        return ""
    value = str(value)
    value = re.sub(r"[\r\n\t]+", " ", value)
    return value[:size]

def table_records(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("projects", "items", "data", "results", "rows", "records"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []

def project_items(rows):
    result = []
    for item in rows[:100]:
        if not isinstance(item, dict):
            continue
        name = next((item[k] for k in ("name", "title", "project_name", "label", "project", "id")
                    if item.get(k) not in (None, "")), None)
        state = next((item[k] for k in ("status", "state", "health", "severity")
                     if item.get(k) is not None), None)
        fresh = next((item[k] for k in ("fresh", "stale", "is_fresh", "is_stale")
                     if type(item.get(k)) is bool), None)
        result.append({"name": safe_label(name), "state": safe_label(state),
                       "freshness_flag": fresh})
    return result

def shapes(data):
    if isinstance(data, list):
        return {"shape": "list", "count": len(data),
                "first_keys": sorted(list(data[0]))[:25] if data and isinstance(data[0], dict) else []}
    if isinstance(data, dict):
        return {"shape": "dict", "keys": sorted(data.keys())[:35],
                "count": len(table_records(data))}
    return {"shape": type(data).__name__}

def panel_positions(panels):
    problems = []
    blocks = []
    for p in panels:
        if not isinstance(p, dict) or p.get("type") == "row":
            continue
        q = p.get("gridPos")
        if not isinstance(q, dict):
            continue
        try:
            x, y, w, h = (int(q[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, ValueError, TypeError):
            problems.append({"issue": "invalid_grid", "panel_id": p.get("id")})
            continue
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 24:
            problems.append({"issue": "out_of_bounds", "panel_id": p.get("id"),
                             "grid": [x, y, w, h]})
        for op, ox, oy, ow, oh in blocks:
            if x < ox + ow and ox < x + w and y < oy + oh and oy < y + h:
                problems.append({"issue": "overlap", "panels": [op, p.get("id")]})
        blocks.append((p.get("id"), x, y, w, h))
    return problems

def all_panels(panels):
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        yield panel
        for child in all_panels(panel.get("panels", [])):
            yield child

def inspect_frames(value, ref_ids):
    results = value.get("results", {}) if isinstance(value, dict) else {}
    summary = []
    for ref_id in ref_ids:
        result = results.get(ref_id, {})
        frames = result.get("frames") or []
        rows, value_count, notices = 0, 0, 0
        for frame in frames:
            schema = frame.get("schema", {})
            data = frame.get("data", {})
            columns = data.get("values") or []
            rows += max((len(col) for col in columns if isinstance(col, list)), default=0)
            types = [field.get("type") for field in schema.get("fields", [])]
            for col, typ in zip(columns, types):
                if typ != "time" and isinstance(col, list):
                    value_count += sum(v is not None and v != "" for v in col)
            notices += len((schema.get("meta") or {}).get("notices") or [])
        err = bool(result.get("error") or result.get("errors"))
        summary.append({"ref": ref_id, "frames": len(frames), "rows": rows,
                        "values": value_count, "notices": notices, "error": err,
                        "status_code": result.get("status", 200)})
    return summary

def check_panel_query(token, item, timeout):
    uid, panel = item
    targets = [copy.deepcopy(t) for t in panel.get("targets", [])
               if isinstance(t, dict) and not t.get("hide")]
    parent_ds = panel.get("datasource")
    for target in targets:
        if not target.get("datasource") and parent_ds:
            target["datasource"] = parent_ds
        target.setdefault("intervalMs", 60000)
        target.setdefault("maxDataPoints", 200)
    title = safe_label(panel.get("title") or panel.get("type"))
    row = {"dashboard": uid, "panel_id": panel.get("id"), "title": title,
           "type": panel.get("type"), "target_count": len(targets)}
    if not targets:
        row["result"] = "NO_TARGET"
        return row
    now = int(time.time() * 1000)
    body = {"from": str(now - 24 * 3600 * 1000), "to": str(now),
            "queries": targets}
    try:
        payload = request_json(GRAFANA + "/api/ds/query", token, body, timeout)
        summary = inspect_frames(payload, [t.get("refId", "A") for t in targets])
        row["query"] = summary
        row["result"] = ("ERROR" if any(s["error"] or int(s["status_code"] or 0) >= 400
                                             for s in summary)
                         else "EMPTY" if not any(s["frames"] and s["values"] for s in summary)
                         else "OK")
    except Exception as exc:
        row.update(result="ERROR", error=error_kind(exc))
    return row

def audit(token, timeout=10, max_queries=130):
    findings, report = [], {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                            "scope": "MOSITLAB wallboard + Perimetr; read-only",
                            "dashboards": [], "sources": [], "projects": {},
                            "playlist": {}, "plugins": {}, "queries": [],
                            "findings": findings}
    def finding(level, code, subject):
        findings.append({"level": level, "code": code, "subject": safe_label(subject)})
    try:
        report["grafana_health"] = request_json(GRAFANA + "/api/health", timeout=timeout)
    except Exception as e:
        report["grafana_health"] = {"error": error_kind(e)}
        finding("FAIL", "GRAFANA_UNAVAILABLE", "Grafana HTTP 3000")
        return report

    try:
        plugin = request_json(GRAFANA + "/api/plugins/" + PANEL_PLUGIN + "/settings", token, timeout=timeout)
        report["plugins"] = {"echarts": plugin.get("id") == PANEL_PLUGIN, "type": plugin.get("type")}
        if not report["plugins"]["echarts"]:
            finding("FAIL", "ECHARTS_MISSING", PANEL_PLUGIN)
    except Exception as e:
        report["plugins"] = {"echarts": False, "error": error_kind(e)}
        finding("FAIL", "ECHARTS_NOT_VERIFIED", PANEL_PLUGIN)

    try:
        playlist = request_json(GRAFANA + PLAYLIST_ROUTE, token, timeout=timeout)
        spec = playlist.get("spec", {})
        items = [item.get("value") for item in spec.get("items", [])]
        report["playlist"] = {"interval": spec.get("interval"), "items": items,
                              "correct": spec.get("interval") == "1m" and items == list(DASHBOARDS)}
        if not report["playlist"]["correct"]:
            finding("FAIL", "PLAYLIST_MISMATCH", PLAYLIST)
    except Exception as e:
        report["playlist"] = {"correct": False, "error": error_kind(e)}
        finding("FAIL", "PLAYLIST_UNAVAILABLE", PLAYLIST)

    try:
        ds = request_json(GRAFANA + "/api/datasources/uid/wallboard-api", token, timeout=timeout)
        report["datasource"] = {
            "uid": ds.get("uid"), "type": ds.get("type"),
            "allowed_local_wallboard": WALLBOARD in (ds.get("jsonData", {}).get("allowedHosts") or [])}
        if not report["datasource"]["allowed_local_wallboard"]:
            finding("FAIL", "WALLBOARD_DATASOURCE_HOST", "wallboard-api")
    except Exception as e:
        report["datasource"] = {"error": error_kind(e)}
        finding("FAIL", "DATASOURCE_UNAVAILABLE", "wallboard-api")

    try:
        listings = request_json(GRAFANA + "/api/search?type=dash-db&limit=1000", token, timeout=timeout)
        report["grafana_dashboard_inventory"] = {
            "total_visible": len(listings) if isinstance(listings, list) else None,
            "titles": [safe_label(x.get("title")) for x in listings[:50] if isinstance(x, dict)]
                      if isinstance(listings, list) else []}
    except Exception as e:
        report["grafana_dashboard_inventory"] = {"error": error_kind(e)}
        finding("INFO", "DASHBOARD_INVENTORY_RESTRICTED", "/api/search")

    work = []
    for uid in DASHBOARDS:
        try:
            resource = request_json(GRAFANA + "/api/dashboards/uid/" + uid, token, timeout=timeout)
            dash = resource.get("dashboard") or {}
            panels = list(all_panels(dash.get("panels", [])))
            layout = panel_positions(dash.get("panels", []))
            row = {"uid": uid, "title": safe_label(dash.get("title")),
                   "version": dash.get("version"), "panel_count": len(panels),
                   "refresh": dash.get("refresh"), "layout_issues": layout,
                   "panel_titles": [safe_label(p.get("title") or p.get("type")) for p in panels]}
            report["dashboards"].append(row)
            for p in panels:
                if p.get("targets"):
                    work.append((uid, p))
            for item in layout:
                finding("WARN", "DASHBOARD_LAYOUT", uid + "/" + str(item.get("issue")))
        except Exception as e:
            report["dashboards"].append({"uid": uid, "error": error_kind(e)})
            finding("FAIL", "DASHBOARD_UNAVAILABLE", uid)

    if len(work) > max_queries:
        finding("WARN", "QUERY_LIMIT", "checked first " + str(max_queries) + " of " + str(len(work)))
    work = work[:max_queries]
    if work:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(check_panel_query, token, item, timeout) for item in work]
            for future in as_completed(futures):
                row = future.result()
                report["queries"].append(row)
                if row["result"] == "ERROR":
                    finding("FAIL", "PANEL_QUERY_FAILED",
                            row["dashboard"] + "/" + str(row["panel_id"]) + " " + row["title"])
                elif row["result"] == "EMPTY":
                    finding("WARN", "PANEL_NO_VALUES",
                            row["dashboard"] + "/" + str(row["panel_id"]) + " " + row["title"])
    report["queries"].sort(key=lambda r: (r["dashboard"], str(r["panel_id"])))

    for route in LOCAL_ROUTES:
        item = {"endpoint": ":19150" + route}
        try:
            value = request_json(WALLBOARD + route, timeout=timeout)
            item["ok"] = True
            item.update(shapes(value))
            if route == "/projects":
                projects = table_records(value)
                report["projects"] = {"count": len(projects), "items": project_items(projects),
                                      "schema": shapes(value)}
                if not projects:
                    finding("WARN", "PROJECT_LIST_EMPTY_OR_UNKNOWN_SCHEMA", "/projects")
            if route == "/perimeter-ha/summary" and isinstance(value, dict):
                item["severity"] = value.get("severity")
                item["active_count"] = value.get("active_count")
                item["ready_reserves"] = value.get("ready_reserves")
                item["leader"] = safe_label(value.get("leader"))
                item["stale"] = value.get("stale")
                item["recent_timestamp_age_s"] = round(time.time() - value["timestamp"], 1) if type(value.get("timestamp")) in (int, float) else None
                if value.get("severity") != 0 or value.get("stale") is True:
                    finding("WARN", "PERIMETER_HA_DEGRADED", "/perimeter-ha/summary")
            if route == "/perimeter-ha/recent":
                records = table_records(value)
                item["real_events"] = sum(isinstance(r, dict) and r.get("event_id") is not None for r in records)
                if not item["real_events"]:
                    finding("WARN", "NO_RECENT_PERIMETER_EVENTS", route)
        except Exception as e:
            item.update(ok=False, error=error_kind(e))
            finding("FAIL", "WALLBOARD_ENDPOINT_FAILED", route)
        report["sources"].append(item)

    try:
        request_json("http://127.0.0.1:19150/perimeter-ha/summary", timeout=timeout)
        report["backend_local"] = True
    except Exception:
        report["backend_local"] = False

    counts = {level: sum(i["level"] == level for i in findings) for level in ("FAIL", "WARN", "INFO")}
    counts["panels_checked"] = len(report["queries"])
    counts["panels_ok"] = sum(i["result"] == "OK" for i in report["queries"])
    counts["dashboards_loaded"] = sum("error" not in x for x in report["dashboards"])
    counts["endpoints_ok"] = sum(i["ok"] for i in report["sources"])
    report["totals"] = counts
    return report

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--timeout", type=int, default=10)
    p.add_argument("--max-queries", type=int, default=130)
    p.add_argument("--report", type=Path, help="Optional private JSON path for full audit")
    args = p.parse_args(argv)
    if not 2 <= args.timeout <= 30 or not 1 <= args.max_queries <= 300:
        p.error("timeout must be 2..30, max-queries 1..300")
    token = token_from_env(Path("/etc/mositlab-wallboard.env"))
    result = audit(token, args.timeout, args.max_queries)
    if args.report:
        fd = args.report.open("x", encoding="utf-8")
        args.report.chmod(0o600)
        with fd:
            json.dump(result, fd, ensure_ascii=False, indent=2)
            fd.write("\n")
    print("=== WALLBOARD AUDIT (READ ONLY) ===")
    print("Grafana:", safe_label(result.get("grafana_health", {}).get("database", "unknown")))
    print("ECharts:", result["plugins"])
    print("Playlist:", result["playlist"])
    for d in result["dashboards"]:
        print("DASHBOARD:", d["uid"], "panels=", d.get("panel_count"),
              "version=", d.get("version"), "layout_issues=", len(d.get("layout_issues", [])),
              "error=", d.get("error", "none"))
    print("PROJECTS:", result["projects"].get("count"), "schema:", result["projects"].get("schema"))
    for x in result["projects"].get("items", []):
        print("PROJECT:", x["name"], "status=", x["state"], "freshness_flag=", x["freshness_flag"])
    for s in result["sources"]:
        print("SOURCE:", s["endpoint"], "ok=", s["ok"], "count=", s.get("count"),
              "severity=", s.get("severity"), "owner=", s.get("leader"),
              "age_s=", s.get("recent_timestamp_age_s"), "error=", s.get("error"))
    for q in result["queries"]:
        print("PANEL:", q["dashboard"], "id=", q["panel_id"], "name=", q["title"],
              "result=", q["result"], "data=", [
                  (x["frames"], x["rows"], x["values"]) for x in q.get("query", [])
              ], "error=", q.get("error"))
    print("=== FINDINGS ===")
    for f in result["findings"]:
        print(f["level"], f["code"], f["subject"])
    print("=== TOTALS ===", result.get("totals", {}))
    if args.report:
        print("PRIVATE_REPORT:", str(args.report))
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("WALLBOARD_AUDIT_FAILED", error_kind(exc), file=sys.stderr)
        raise SystemExit(2)
