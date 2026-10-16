"""Pure dashboard/playlist builder. Existing projects and datasources are retained."""
from __future__ import annotations

import copy
from pathlib import Path

OVERVIEW_UID = "mositlab-director-wallboard"
VISUAL_UID = "mositlab-perimeter-visual"
PLAYLIST_UID = "mositlab-perimeter-wallboard"
MANAGED_TAG = "managed-perimeter-visual-v1"
DS = {"type": "yesoreyeram-infinity-datasource", "uid": "wallboard-api"}
BASE = "http://127.0.0.1:19150/perimeter-ha"


def target(route, columns, ref="A"):
    return {"refId": ref, "datasource": DS, "type": "json", "source": "url", "parser": "backend",
            "format": "table", "root_selector": "", "url": BASE + route,
            "url_options": {"method": "GET"},
            "columns": [{"selector": key, "text": label, "type": kind} for key, label, kind in columns]}


def status_mappings():
    states = {"0": ("РАБОТАЕТ", "green"), "1": ("ВНИМАНИЕ", "yellow"), "2": ("ОТКАЗ / НЕТ ДАННЫХ", "red")}
    return [{"type": "value", "options": {key: {"text": text, "color": color} for key, (text, color) in states.items()}}]


def stat(panel_id, title, field, x, width=4, kind="number", mappings=None):
    return {"id": panel_id, "type": "stat", "title": title, "datasource": DS,
            "gridPos": {"x": x, "y": 0, "w": width, "h": 3},
            "fieldConfig": {"defaults": {"noValue": "—", "color": {"mode": "fixed", "fixedColor": "text"},
                                         "mappings": mappings or []}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "textMode": "value", "graphMode": "none", "justifyMode": "center"},
            "targets": [target("/summary", [(field, field, kind)])]}


def visual_dashboard(existing=None):
    if existing and MANAGED_TAG not in existing.get("tags", []):
        raise ValueError("ExistingVisualDashboardCollision")
    script = Path(__file__).with_name("perimeter_topology.js").read_text(encoding="utf-8")
    dashboard = {"uid": VISUAL_UID, "title": "Периметр • работа и резервирование", "schemaVersion": 39,
                 "version": existing.get("version", 0) if existing else 0,
                 "editable": True, "tags": ["perimeter", MANAGED_TAG], "timezone": "Europe/Moscow",
                 "time": {"from": "now-24h", "to": "now"}, "refresh": "5s", "panels": [],
                 "links": [{"title": "Общий мониторинг", "type": "link", "url": "/d/" + OVERVIEW_UID + "?kiosk", "targetBlank": False},
                           {"title": "Чередование • 1 мин", "type": "link", "url": "/playlists/play/" + PLAYLIST_UID + "?kiosk", "targetBlank": False}]}
    if existing and existing.get("id"):
        dashboard["id"] = existing["id"]
    panels = [stat(1, "Состояние", "severity", 0, 4, mappings=status_mappings()),
              stat(2, "Активный узел", "leader", 4, 4, "string"),
              stat(3, "Въезд • 24 ч", "in_24h", 8), stat(4, "Выезд • 24 ч", "out_24h", 12),
              stat(5, "Только склад • 24 ч", "warehouse_only_24h", 16),
              stat(6, "Резервы готовы", "ready_reserves", 20)]
    columns = [(name, name, "number" if name in {"x", "y", "w", "h"} else "string")
               for name in ("id", "kind", "label", "detail", "state", "x", "y", "w", "h", "source", "target")]
    panels.append({"id": 10, "type": "volkovlabs-echarts-panel", "title": "", "datasource": DS,
                   "gridPos": {"x": 0, "y": 3, "w": 24, "h": 18},
                   "options": {"renderer": "svg", "getOption": script},
                   "fieldConfig": {"defaults": {}, "overrides": []}, "targets": [target("/diagram", columns)]})
    queue_columns = [("node", "Узел", "string"),
                     ("rfid", "RFID очередь", "number"), ("video", "YOLO очередь", "number"),
                     ("fallback_pending", "К выгрузке", "number"),
                     ("cache_days", "Дней копии", "number"),
                     ("cache_state", "Копия", "string"),
                     ("cache_age", "Возраст копии, с", "number")]
    recent_columns = [("time", "Время", "string"), ("direction", "Направление", "string"),
                      ("reel", "Катушка / метка", "string"), ("reads", "RFID чтений", "number"),
                      ("video", "Видео", "string"), ("warehouse", "Склад", "string"), ("state", "Подтверждение", "string")]
    for panel_id, title, route, cols, x, width in (
        (11, "Очереди и копии • все узлы", "/queues", queue_columns, 0, 9),
        (12, "Последние въезды / выезды", "/recent", recent_columns, 9, 15),
    ):
        panels.append({"id": panel_id, "type": "table", "title": title, "datasource": DS,
                       "gridPos": {"x": x, "y": 21, "w": width, "h": 7},
                       "options": {"showHeader": True, "cellHeight": "sm", "sortBy": []},
                       "fieldConfig": {"defaults": {"noValue": "—", "custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                                       "overrides": [
                                           {"matcher": {"id": "byName", "options": "Направление"}, "properties": [
                                               {"id": "mappings", "value": [{"type": "value", "options": {
                                                   "ВЪЕЗД": {"text": "↓ ВЪЕЗД", "color": "green"},
                                                   "ВЫЕЗД": {"text": "↑ ВЫЕЗД", "color": "blue"},
                                                   "СКЛАД": {"text": "◆ СКЛАД", "color": "yellow"}}}]},
                                               {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
                                           {"matcher": {"id": "byName", "options": "Подтверждение"}, "properties": [
                                               {"id": "mappings", "value": [{"type": "value", "options": {
                                                   "КОНФЛИКТ": {"text": "КОНФЛИКТ", "color": "red"},
                                                   "УСТАРЕЛО": {"text": "УСТАРЕЛО", "color": "yellow"},
                                                   "RFID": {"text": "RFID", "color": "green"},
                                                   "ТОЛЬКО СКЛАД": {"text": "ТОЛЬКО СКЛАД", "color": "yellow"}}}]},
                                               {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
                                       ]}, "targets": [target(route, cols)]})
    dashboard["panels"] = panels
    return dashboard


def remove_top_banner(dashboard, panel_id=None):
    result = copy.deepcopy(dashboard)
    panels = result.get("panels", [])
    candidates = [panel for panel in panels if panel.get("type") == "text"
                  and panel.get("gridPos", {}).get("w", 0) >= 20 and panel.get("gridPos", {}).get("h", 99) <= 6]
    if panel_id is not None:
        candidates = [panel for panel in candidates if panel.get("id") == panel_id]
        if len(candidates) != 1:
            raise ValueError("BannerPanelNotFound")
    elif candidates:
        top = min(panel.get("gridPos", {}).get("y", 99999) for panel in panels
                  if not str(panel.get("description", "")).startswith("Managed by Perimeter"))
        candidates = [panel for panel in candidates if panel.get("gridPos", {}).get("y") == top]
    if not candidates:
        return result, []
    selected = min(candidates, key=lambda panel: panel["gridPos"]["y"])
    y, height = selected["gridPos"]["y"], selected["gridPos"]["h"]
    result["panels"] = [panel for panel in panels if panel["id"] != selected["id"]]
    def shift(rows):
        for panel in rows:
            if panel.get("gridPos", {}).get("y", -1) > y:
                panel["gridPos"]["y"] -= height
            shift(panel.get("panels", []))
    shift(result["panels"])
    return result, [selected["id"]]


def playlist(existing=None):
    if existing and existing.get("spec", {}).get("title") != "MOSITLAB • общий / Периметр":
        raise ValueError("ExistingPlaylistCollision")
    metadata = {"name": PLAYLIST_UID}
    if existing:
        metadata.update({key: existing["metadata"][key] for key in ("resourceVersion", "namespace") if key in existing.get("metadata", {})})
    return {"kind": "Playlist", "apiVersion": "playlist.grafana.app/v1", "metadata": metadata,
            "spec": {"title": "MOSITLAB • общий / Периметр", "interval": "1m",
                     "items": [{"type": "dashboard_by_uid", "value": uid} for uid in (OVERVIEW_UID, VISUAL_UID)]}}
