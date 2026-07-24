#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
КПП • WEB v2.4: промышленный мониторинг движения катушек.

Новый файл ДОПОЛНЯЕТ существующие сервисы и использует уже собранные данные:
- dbo.KPP_ReelEvents
- dbo.KPP_RuntimeState
- dbo.ReelTransitions (для превью изображения по VideoEventId)

Особенности:
- Светлая современная минималистичная тема
- Сводные карточки, графики, фильтры, таймлайн событий
- Просмотр деталей события в модальном окне
- Пояснение логики принятия решения
- Не мешает существующим скриптам мониторинга, работает только на чтение
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pyodbc
from flask import Flask, Response, jsonify, render_template_string, request


# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================


class Config:
    HOST = os.getenv("KPP_WEB_HOST", "0.0.0.0")
    PORT = int(os.getenv("KPP_WEB_PORT", "5050"))
    DEBUG = os.getenv("KPP_WEB_DEBUG", "0") == "1"
    AUTO_REFRESH_SEC = int(os.getenv("KPP_WEB_REFRESH_SEC", "10"))
    DEFAULT_LIMIT = int(os.getenv("KPP_WEB_DEFAULT_LIMIT", "200"))
    MAX_LIMIT = int(os.getenv("KPP_WEB_MAX_LIMIT", "1000"))
    SUMMARY_HOURS = int(os.getenv("KPP_WEB_SUMMARY_HOURS", "24"))
    CHART_DAYS = int(os.getenv("KPP_WEB_CHART_DAYS", "3"))

    DB_CONN_STR = os.getenv(
        "KPP_WEB_DB_CONNECTION",
        os.getenv(
            "RFID_DB_CONNECTION",
            "DRIVER={ODBC Driver 18 for SQL Server};"
            "SERVER=SRV-SQL4.MKM.LAN;"
            "DATABASE=1CTgSend;"
            "UID=TgSendUser;"
            "PWD=Shu_uc3i;"
            "Encrypt=yes;"
            "TrustServerCertificate=yes;",
        ),
    )
    TASK_TABLE = os.getenv("KPP_TASK_TABLE", "dbo.RfidTags")


app = Flask(__name__)


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================


def db_connect() -> pyodbc.Connection:
    return pyodbc.connect(Config.DB_CONN_STR, timeout=10)


class JsonEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def parse_json_safe(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}


def row_to_dict(cursor: pyodbc.Cursor, row: pyodbc.Row) -> Dict[str, Any]:
    columns = [col[0] for col in cursor.description]
    data = dict(zip(columns, row))
    if "EvidenceJson" in data:
        data["EvidenceJsonParsed"] = parse_json_safe(data.get("EvidenceJson"))
    return data


def format_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ============================================================================
# SQL-ЗАПРОСЫ
# ============================================================================


def fetch_runtime_state() -> Dict[str, Any]:
    query = """
    SELECT StateKey, StateValue, UpdatedAt
    FROM dbo.KPP_RuntimeState
    """
    state: Dict[str, Any] = {}
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(query)
        for key, value, updated_at in cur.fetchall():
            state[str(key)] = {
                "value": value,
                "updated_at": updated_at.isoformat() if updated_at else None,
            }
    return state


def fetch_summary(hours: int) -> Dict[str, Any]:
    query = """
    WITH src AS (
        SELECT *
        FROM dbo.KPP_ReelEvents
        WHERE FirstSeen >= DATEADD(hour, -?, GETDATE())
    )
    SELECT
        COUNT(*)                                           AS TotalEvents,
        SUM(CASE WHEN NeedRecheck = 1 THEN 1 ELSE 0 END)  AS PendingRecheck,
        SUM(CASE WHEN FinalizedAt IS NOT NULL THEN 1 ELSE 0 END) AS FinalizedEvents,
        SUM(CASE WHEN Task1CId IS NOT NULL THEN 1 ELSE 0 END)    AS Matched1C,
        SUM(CASE WHEN VideoMatched = 1 THEN 1 ELSE 0 END)        AS VideoMatched,
        SUM(CASE WHEN SkudMatched = 1 THEN 1 ELSE 0 END)         AS SkudMatched,
        AVG(CAST(ConfidencePct AS FLOAT)) AS AvgConfidence,
        SUM(CASE WHEN FinalDirection = 'IN' THEN 1 ELSE 0 END)   AS DirIn,
        SUM(CASE WHEN FinalDirection = 'OUT' THEN 1 ELSE 0 END)  AS DirOut,
        SUM(CASE WHEN FinalDirection = 'UNKNOWN' THEN 1 ELSE 0 END) AS DirUnknown,
        SUM(CASE WHEN TaskMatchType = 'FULL_TAG' THEN 1 ELSE 0 END) AS MatchFullTag,
        SUM(CASE WHEN TaskMatchType = 'EPC_ONLY' THEN 1 ELSE 0 END) AS MatchEpcOnly,
        SUM(CASE WHEN TaskMatchType = 'NOT_FOUND' THEN 1 ELSE 0 END) AS MatchNotFound,
        SUM(CASE WHEN TaskMatchType IN ('FULL_TAG_WAREHOUSE', 'EPC_ONLY_WAREHOUSE', 'WAREHOUSE_ONLY') THEN 1 ELSE 0 END) AS MatchWarehouse,
        SUM(CASE WHEN SessionCloseReason = 'WAREHOUSE_ONLY' THEN 1 ELSE 0 END) AS WarehouseOnlyEvents
    FROM src
    """
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(query, hours)
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "total_events": int(row[0] or 0),
            "pending_recheck": int(row[1] or 0),
            "finalized_events": int(row[2] or 0),
            "matched_1c": int(row[3] or 0),
            "video_matched": int(row[4] or 0),
            "skud_matched": int(row[5] or 0),
            "avg_confidence": round(float(row[6] or 0), 1),
            "dir_in": int(row[7] or 0),
            "dir_out": int(row[8] or 0),
            "dir_unknown": int(row[9] or 0),
            "match_full_tag": int(row[10] or 0),
            "match_epc_only": int(row[11] or 0),
            "match_not_found": int(row[12] or 0),
            "match_warehouse": int(row[13] or 0),
            "warehouse_only_events": int(row[14] or 0),
        }


def fetch_chart_series(days: int) -> Dict[str, Any]:
    query = """
    SELECT
        CONVERT(varchar(16), DATEADD(minute, DATEDIFF(minute, 0, FirstSeen) / 30 * 30, 0), 120) AS Bucket,
        SUM(CASE WHEN FinalDirection = 'IN' THEN 1 ELSE 0 END) AS DirIn,
        SUM(CASE WHEN FinalDirection = 'OUT' THEN 1 ELSE 0 END) AS DirOut,
        SUM(CASE WHEN FinalDirection = 'UNKNOWN' THEN 1 ELSE 0 END) AS DirUnknown,
        SUM(CASE WHEN NeedRecheck = 1 THEN 1 ELSE 0 END) AS Pending,
        AVG(CAST(ConfidencePct AS FLOAT)) AS AvgConfidence
    FROM dbo.KPP_ReelEvents
    WHERE FirstSeen >= DATEADD(day, -?, GETDATE())
    GROUP BY DATEADD(minute, DATEDIFF(minute, 0, FirstSeen) / 30 * 30, 0)
    ORDER BY DATEADD(minute, DATEDIFF(minute, 0, FirstSeen) / 30 * 30, 0)
    """
    rows: List[Dict[str, Any]] = []
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(query, days)
        for bucket, dir_in, dir_out, dir_unknown, pending, avg_conf in cur.fetchall():
            rows.append(
                {
                    "bucket": bucket,
                    "dir_in": int(dir_in or 0),
                    "dir_out": int(dir_out or 0),
                    "dir_unknown": int(dir_unknown or 0),
                    "pending": int(pending or 0),
                    "avg_confidence": round(float(avg_conf or 0), 1),
                }
            )
    return {"series": rows}


def fetch_top_warnings(hours: int) -> List[Dict[str, Any]]:
    query = """
    SELECT WarningFlags
    FROM dbo.KPP_ReelEvents
    WHERE FirstSeen >= DATEADD(hour, -?, GETDATE())
      AND ISNULL(WarningFlags, '') <> ''
    """
    counter: Dict[str, int] = {}
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(query, hours)
        for (warning_flags,) in cur.fetchall():
            raw = str(warning_flags or "")
            parts: List[str] = []
            for chunk in raw.replace(" | ", "|").split("|"):
                item = chunk.strip()
                if item:
                    parts.append(item)
            if not parts and raw.strip():
                parts = [raw.strip()]
            for item in parts:
                counter[item] = counter.get(item, 0) + 1
    return [
        {"warning": warning, "count": count}
        for warning, count in sorted(counter.items(), key=lambda x: (-x[1], x[0]))[:10]
    ]

def fetch_events(
    limit: int,
    direction: str = "",
    consensus: str = "",
    match_type: str = "",
    transport: str = "",
    pending: str = "",
    search: str = "",
) -> List[Dict[str, Any]]:
    limit = max(1, min(limit, Config.MAX_LIMIT))
    where = ["1=1"]
    params: List[Any] = []

    if direction:
        where.append("FinalDirection = ?")
        params.append(direction)
    if consensus:
        where.append("ConsensusCode = ?")
        params.append(consensus)
    if match_type:
        where.append("TaskMatchType = ?")
        params.append(match_type)
    if transport:
        where.append("TransportMode = ?")
        params.append(transport)
    if pending == "1":
        where.append("NeedRecheck = 1")
    elif pending == "0":
        where.append("NeedRecheck = 0")
    if search:
        where.append(
            "("
            "ISNULL(e.EventKey, '') LIKE ? OR "
            "ISNULL(e.SourceTag, '') LIKE ? OR "
            "ISNULL(e.EPC, '') LIKE ? OR "
            "ISNULL(e.TID, '') LIKE ? OR "
            "ISNULL(e.Task1CDocIds, '') LIKE ? OR "
            "ISNULL(CAST(e.Task1CId AS nvarchar(50)), '') LIKE ? OR "
            "ISNULL(t.SeriesNumber, '') LIKE ? OR "
            "ISNULL(JSON_VALUE(e.EvidenceJson, '$.warehouse.series_number'), '') LIKE ? OR "
            "ISNULL(JSON_VALUE(e.EvidenceJson, '$.warehouse.doc_ids'), '') LIKE ? OR "
            "ISNULL(JSON_VALUE(e.EvidenceJson, '$.warehouse.tag'), '') LIKE ?"
            ")"
        )
        like = f"%{search}%"
        params.extend([like, like, like, like, like, like, like, like, like, like])

    query = f"""
    SELECT TOP (?)
        e.EventId,
        e.EventKey,
        e.SourceTag,
        e.EPC,
        e.TID,
        e.Task1CId,
        e.Task1CDt,
        e.Task1CDocIds,
        t.SeriesNumber AS Task1CSeriesNumber,
        e.TaskMatchType,
        e.FirstSeen,
        e.LastSeen,
        e.CompletedAt,
        e.SessionCloseReason,
        e.RfidReadCount,
        e.DistinctAntennaCount,
        e.DistinctZoneCount,
        e.RfidFirstAntenna,
        e.RfidLastAntenna,
        e.FirstZone,
        e.LastZone,
        e.RfidAntennasCsv,
        e.RfidZonesCsv,
        e.AvgRSSI,
        e.MinRSSI,
        e.MaxRSSI,
        e.DurationMs,
        e.RfidDirection,
        e.RfidDirectionScore,
        e.VideoMatched,
        e.VideoEventId,
        e.VideoTime,
        e.VideoDirection,
        e.VideoTransport,
        e.VideoTimeDeltaMs,
        e.VideoScore,
        e.SkudMatched,
        e.SkudExternalId,
        e.SkudTime,
        e.SkudDirection,
        e.SkudGate,
        e.SkudPerson,
        e.SkudCard,
        e.SkudTimeDeltaMs,
        e.SkudScore,
        e.FinalDirection,
        e.ConfidencePct,
        e.ConsensusCode,
        e.ScoreIn,
        e.ScoreOut,
        e.SourceCount,
        e.TransportMode,
        e.WarningFlags,
        e.EvidenceJson,
        JSON_VALUE(e.EvidenceJson, '$.warehouse.warehouse_id') AS WarehouseId,
        JSON_VALUE(e.EvidenceJson, '$.warehouse.dt') AS WarehouseDt,
        JSON_VALUE(e.EvidenceJson, '$.warehouse.doc_ids') AS WarehouseDocIds,
        JSON_VALUE(e.EvidenceJson, '$.warehouse.series_number') AS WarehouseSeriesNumber,
        JSON_VALUE(e.EvidenceJson, '$.warehouse.match_type') AS WarehouseMatchType,
        e.NeedRecheck,
        e.NextRecheckAt,
        e.RecheckCount,
        e.LastRecheckAt,
        e.CreatedAt,
        e.UpdatedAt,
        e.FinalizedAt
    FROM dbo.KPP_ReelEvents e
    LEFT JOIN {Config.TASK_TABLE} t ON t.Id = e.Task1CId
    WHERE {' AND '.join(where)}
    ORDER BY e.FirstSeen DESC, e.EventId DESC
    """

    events: List[Dict[str, Any]] = []
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(query, [limit, *params])
        for row in cur.fetchall():
            data = row_to_dict(cur, row)
            events.append(data)
    return events


def fetch_event_details(event_id: int) -> Optional[Dict[str, Any]]:
    query = f"""
    SELECT
        e.*, 
        t.SeriesNumber AS Task1CSeriesNumber
    FROM dbo.KPP_ReelEvents e
    LEFT JOIN {Config.TASK_TABLE} t ON t.Id = e.Task1CId
    WHERE e.EventId = ?
    """
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(query, event_id)
        row = cur.fetchone()
        if not row:
            return None
        return row_to_dict(cur, row)


# ============================================================================
# WEB
# ============================================================================


PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>КПП</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      --bg: #0f141b;
      --surface: #ffffff;
      --surface-2: #f3f5f7;
      --border: #c9d1db;
      --text: #111827;
      --muted: #5b6472;
      --accent: #1f4e79;
      --accent-soft: #e8f1f8;
      --good: #0f7a3a;
      --good-soft: #e7f4ec;
      --warn: #a15c00;
      --warn-soft: #fff1db;
      --bad: #b91c1c;
      --bad-soft: #fae8e8;
      --shadow: 0 2px 10px rgba(15, 23, 42, 0.14);
      --radius: 8px;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      background: #e6ebf0;
      color: var(--text);
    }
    .page {
      max-width: 1680px;
      margin: 0 auto;
      padding: 22px;
    }
    .header {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 16px;
      align-items: center;
      margin-bottom: 20px;
    }
    .title-block {
      /* Убираем темный градиент, так как текст теперь темный, или оставляем фон светлым */
      background: #ffffff; 
      color: #1f4e79; /* Основной цвет текста блока */
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 24px;
    }
    .title {
      margin: 0 0 8px 0;
      font-size: 30px;
      font-weight: 800;
      letter-spacing: -0.02em;
      color: #1f4e79; /* Явно задаем цвет заголовка */
    }
    .subtitle {
      color: #1f4e79; /* Цвет подзаголовка */
      font-size: 14px;
      line-height: 1.5;
      opacity: 0.8; /* Можно добавить прозрачность, если хотите сделать его чуть светлее основного цвета */
    }
    .runtime {
      display: grid;
      gap: 12px;
      min-width: 260px;
    }
    .runtime-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 18px 20px;
    }
    .runtime-label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }
    .runtime-value {
      font-size: 22px;
      font-weight: 800;
    }
    .hint {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
    }
    .grid-cards {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }
    .metric-card {
      padding: 18px 18px 16px;
    }
    .metric-label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 10px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .metric-value {
      font-size: 28px;
      font-weight: 800;
      letter-spacing: -0.03em;
    }
    .metric-sub {
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
    }
    .accent { color: var(--accent); }
    .good { color: var(--good); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }

    .panel-grid {
      display: grid;
      grid-template-columns: 1.25fr 1fr;
      gap: 16px;
      margin-bottom: 18px;
    }
    .panel {
      padding: 18px;
    }
    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 14px;
    }
    .panel-title {
      font-size: 18px;
      font-weight: 800;
      margin: 0;
    }
    .panel-desc {
      color: var(--muted);
      font-size: 13px;
      margin-top: 4px;
    }
    .chart-wrap {
      position: relative;
      height: 320px;
    }
    .mini-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .warning-list,
    .logic-list {
      display: grid;
      gap: 10px;
    }
    .warning-item,
    .logic-item {
      padding: 14px;
      border-radius: 8px;
      background: var(--surface-2);
      border: 1px solid var(--border);
    }
    .warning-item strong,
    .logic-item strong {
      display: block;
      margin-bottom: 6px;
    }
    .logic-flow {
      display: grid;
      grid-template-columns: 1fr auto 1fr auto 1fr auto 1fr auto 1.2fr;
      gap: 8px;
      align-items: stretch;
      margin-bottom: 12px;
    }
    .flow-node {
      border: 1px solid #9aa7b5;
      background: #f6f8fa;
      padding: 12px;
      font-weight: 800;
      text-align: center;
      text-transform: uppercase;
      font-size: 12px;
    }
    .flow-node span { display:block; margin-top:4px; font-weight:600; color:var(--muted); text-transform:none; }
    .flow-node.final { background:#1f4e79; color:#fff; border-color:#1f4e79; }
    .flow-node.final span { color:#dceaf5; }
    .flow-arrow { display:flex; align-items:center; justify-content:center; color:#4b5563; font-weight:900; }
    .filters {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr)) 1.3fr auto auto;
      gap: 12px;
      padding: 16px;
      margin-bottom: 18px;
    }
    .field {
      display: grid;
      gap: 6px;
    }
    .field label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }
    select, input {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #fff;
      padding: 10px 12px;
      font-size: 14px;
      color: var(--text);
      outline: none;
    }
    select:focus, input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 4px rgba(47, 107, 255, 0.10);
    }
    button {
      border: 0;
      border-radius: 6px;
      padding: 10px 14px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      transition: 0.18s ease;
    }
    .btn-primary {
      background: var(--accent);
      color: white;
    }
    .btn-primary:hover { filter: brightness(0.96); }
    .btn-light {
      background: white;
      color: var(--text);
      border: 1px solid var(--border);
    }
    .btn-light:hover { background: #f8fbff; }

    .table-card { padding: 0; overflow: hidden; }
    .table-head {
      padding: 18px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
    }
    .table-wrap {
      overflow: auto;
      max-height: 900px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 1400px;
    }
    th, td {
      border-bottom: 1px solid var(--border);
      padding: 12px 14px;
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }
    th {
      position: sticky;
      top: 0;
      background: #fbfdff;
      z-index: 2;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    tr:hover td { background: #fbfdff; }
    .mono { font-family: Consolas, Menlo, monospace; }
    .tag {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .tag-out { background: var(--bad-soft); color: var(--bad); }
    .tag-in { background: var(--good-soft); color: var(--good); }
    .tag-unknown { background: #eef2f7; color: #4b5563; }
    .tag-pending { background: var(--warn-soft); color: var(--warn); }
    .tag-ok { background: var(--good-soft); color: var(--good); }
    .tag-match { background: var(--accent-soft); color: var(--accent); }
    .tag-not-found { background: #f5f7fb; color: #6b7280; }

    .confidence {
      min-width: 100px;
    }
    .bar {
      width: 100%;
      height: 8px;
      border-radius: 999px;
      background: #edf1f7;
      overflow: hidden;
      margin-top: 6px;
    }
    .bar > span {
      display: block;
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, #2f6bff, #69a2ff);
    }
    .actions {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .link-btn {
      background: transparent;
      color: var(--accent);
      padding: 0;
    }
    .footer-note {
      margin-top: 16px;
      color: var(--muted);
      font-size: 12px;
      text-align: right;
    }

    .modal {
      position: fixed;
      inset: 0;
      background: rgba(17, 24, 39, 0.42);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      z-index: 30;
    }
    .modal.open { display: flex; }
    .modal-card {
      width: min(1540px, 100%);
      max-height: 90vh;
      overflow: auto;
      background: white;
      border-radius: 22px;
      box-shadow: 0 24px 60px rgba(15, 23, 42, 0.25);
      border: 1px solid var(--border);
    }
    .modal-head {
      padding: 20px 22px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }
    .modal-title {
      font-size: 22px;
      font-weight: 800;
      margin: 0;
    }
    .modal-sub {
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }
    .modal-body {
      padding: 22px;
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
    }
    .detail-card {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      background: var(--surface-2);
    }
    .detail-title {
      font-size: 15px;
      font-weight: 800;
      margin: 0 0 12px 0;
    }
    .detail-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px 14px;
    }
    .detail-item small {
      display: block;
      color: var(--muted);
      margin-bottom: 4px;
    }
    .detail-item span {
      font-size: 14px;
      font-weight: 600;
      word-break: break-word;
    }
    .img-box {
      border-radius: 8px;
      overflow: hidden;
      border: 1px solid var(--border);
      background: #f8fbff;
      min-height: 280px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--muted);
    }
    .img-box img {
      width: 100%;
      height: auto;
      display: block;
    }

    .manager-view {
      display: grid;
      grid-template-columns: 0.9fr 1.1fr;
      gap: 18px;
      align-items: stretch;
      margin-bottom: 18px;
    }
    .manager-hero {
      border: 1px solid #aeb8c4;
      border-radius: 10px;
      padding: 18px;
      background: linear-gradient(180deg, #f8fafc 0%, #eef3f7 100%);
    }
    .manager-decision {
      font-size: 34px;
      font-weight: 900;
      letter-spacing: -0.04em;
      margin-bottom: 8px;
      text-transform: uppercase;
    }
    .manager-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-top: 14px;
    }
    .manager-kpi {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      padding: 12px;
    }
    .manager-kpi small { display:block; color:var(--muted); margin-bottom:6px; }
    .manager-kpi strong { display:block; font-size:18px; }
    .source-strip {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 14px;
    }
    .source-pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 8px 10px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: #fff;
      font-weight: 800;
      font-size: 12px;
      text-transform: uppercase;
    }
    .source-pill.ok { border-color: #90c7a2; color: #0f7a3a; background: #f0faf3; }
    .source-pill.bad { border-color: #d7dde5; color: #687385; background: #f8fafc; }
    .source-pill.warn { border-color: #f1c27d; color: #925800; background: #fff7e8; }
    .signal-bars {
      display: inline-flex;
      align-items: flex-end;
      gap: 3px;
      height: 18px;
      margin-right: 8px;
      vertical-align: middle;
    }
    .signal-bars span {
      width: 5px;
      border-radius: 2px 2px 0 0;
      background: #d3dae3;
      display: block;
    }
    .signal-bars span:nth-child(1) { height: 5px; }
    .signal-bars span:nth-child(2) { height: 8px; }
    .signal-bars span:nth-child(3) { height: 11px; }
    .signal-bars span:nth-child(4) { height: 14px; }
    .signal-bars span:nth-child(5) { height: 17px; }
    .signal-bars span.active { background: #1f4e79; }
    .image-large {
      min-height: 430px;
      background: #111827;
    }
    .image-large img { width: 100%; height: 100%; object-fit: contain; max-height: 620px; }
    .expert-section {
      display: grid;
      gap: 12px;
    }
    details.expert-block {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-2);
      overflow: hidden;
    }
    details.expert-block > summary {
      cursor: pointer;
      padding: 14px 16px;
      font-weight: 900;
      list-style: none;
      background: #eef2f7;
      border-bottom: 1px solid transparent;
    }
    details.expert-block[open] > summary { border-bottom-color: var(--border); }
    details.expert-block > summary::-webkit-details-marker { display: none; }
    details.expert-block > summary::after { content: ' раскрыть'; color: var(--muted); font-weight: 600; font-size: 12px; }
    details.expert-block[open] > summary::after { content: ' свернуть'; }
    .expert-content { padding: 16px; }
    .json-toggle pre { max-height: 420px; }
    pre {
      background: #0f172a;
      color: #e5edf8;
      padding: 16px;
      border-radius: 8px;
      overflow: auto;
      font-size: 12px;
      line-height: 1.5;
      margin: 0;
    }
    .tag-warehouse { background: #e0f2fe; color: #075985; border: 1px solid #7dd3fc; }
    .empty {
      padding: 28px;
      text-align: center;
      color: var(--muted);
    }
    @media (max-width: 1280px) {
      .grid-cards { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .panel-grid { grid-template-columns: 1fr; }
      .filters { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .modal-body { grid-template-columns: 1fr; }
    }
    @media (max-width: 760px) {
      .page { padding: 12px; }
      .header { grid-template-columns: 1fr; }
      .grid-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .filters { grid-template-columns: 1fr; }
      .mini-grid, .detail-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="header">
      <div class="title-block card">
        <h1 class="title">КПП</h1>
        <div class="subtitle">Промышленный мониторинг движения катушек</div>
      </div>
      <div class="runtime">
        <div class="runtime-card card">
          <div class="runtime-label">LAST_RFID_ID</div>
          <div class="runtime-value mono" id="lastRfidId">—</div>
          <div class="hint" id="lastRfidUpdated">—</div>
        </div>
        <div class="runtime-card card">
          <div class="runtime-label">Обновление экрана</div>
          <div class="runtime-value accent">каждые {{ refresh_sec }} сек</div>
          <div class="hint" id="serverTime">—</div>
        </div>
      </div>
    </div>

    <div class="grid-cards" id="metrics"></div>

    <div class="panel-grid">
      <div class="card panel">
        <div class="panel-header">
          <div>
            <h2 class="panel-title">Динамика событий</h2>
            <div class="panel-desc">Въезды, выезды и неопределенные события по 30-минутным интервалам. Линия «ждут ответа» скрыта по умолчанию.</div>
          </div>
        </div>
        <div class="chart-wrap">
          <canvas id="timelineChart"></canvas>
        </div>
      </div>
      <div class="mini-grid">
        <div class="card panel">
          <div class="panel-header">
            <div>
              <h2 class="panel-title">Тип матчинга с 1С</h2>
              <div class="panel-desc">Полная метка, EPC и Warehouse. «Не найдено» скрыто по умолчанию, но доступно фильтром.</div>
            </div>
          </div>
          <div class="chart-wrap" style="height: 250px;"><canvas id="matchChart"></canvas></div>
        </div>
        <div class="card panel">
          <div class="panel-header">
            <div>
              <h2 class="panel-title">Частые предупреждения</h2>
              <div class="panel-desc">Повторяющиеся причины: разбираются по отдельным предупреждениям, а не целой строкой.</div>
            </div>
          </div>
          <div class="warning-list" id="warnings"></div>
        </div>
        <div class="card panel" style="grid-column: 1 / -1;">
          <div class="panel-header">
            <div>
              <h2 class="panel-title">Логика принятия решения</h2>
              <div class="panel-desc">Как читать итоговое направление.</div>
            </div>
          </div>
          <div class="logic-flow">
            <div class="flow-node">RFID<br><span>метка катушки</span></div>
            <div class="flow-arrow">→</div>
            <div class="flow-node">Видео<br><span>камера 1/2</span></div>
            <div class="flow-arrow">→</div>
            <div class="flow-node">СКУД<br><span>погрузчик</span></div>
            <div class="flow-arrow">→</div>
            <div class="flow-node">1С + Warehouse<br><span>катушка / склад</span></div>
            <div class="flow-arrow">→</div>
            <div class="flow-node final">KPP_ReelEvents<br><span>итог для отчетности</span></div>
          </div>
          <div class="logic-list">
            <div class="logic-item"><strong>1. RFID-сессия</strong> Считывания одной метки группируются по времени. По внешним/внутренним антеннам оценивается направление.</div>
            <div class="logic-item"><strong>2. Видео и СКУД</strong> Камеры дают факт катушки и направление между камерами. RusGuard подтверждает движение погрузчика/транспорта.</div>
            <div class="logic-item"><strong>3. 1С и Warehouse</strong> Метка считается полезной, если она есть в 1С/RfidTags или Warehouse. 1С может прийти в окне ±24 часа.</div>
            <div class="logic-item"><strong>4. Склад без КПП</strong> Если Warehouse увидел катушку, но КПП-проезд не найден, создается отдельная строка <span class="mono">WAREHOUSE_ONLY</span> в KPP_ReelEvents.</div>
          </div>
        </div>
      </div>
    </div>

    <div class="card filters">
      <div class="field">
        <label>Направление</label>
        <select id="fDirection">
          <option value="">Все</option>
          <option value="IN">ВЪЕЗД</option>
          <option value="OUT">ВЫЕЗД</option>
          <option value="UNKNOWN">НЕИЗВЕСТНО</option>
        </select>
      </div>
      <div class="field">
        <label>Консенсус</label>
        <select id="fConsensus">
          <option value="">Все</option>
          <option value="UNANIMOUS">ЕДИНОГЛАСНО</option>
          <option value="SINGLE">ОДИН ИСТОЧНИК</option>
          <option value="NO_DATA">НЕТ ДАННЫХ</option>
          <option value="SPLIT">РАЗНОГЛАСИЕ</option>
        </select>
      </div>
      <div class="field">
        <label>Матчинг 1С</label>
        <select id="fMatchType">
          <option value="">Все</option>
          <option value="FULL_TAG">ПОЛНАЯ МЕТКА</option>
          <option value="EPC_ONLY">ТОЛЬКО EPC</option>
          <option value="FULL_TAG_WAREHOUSE">ПОЛНАЯ + СКЛАД</option>
          <option value="EPC_ONLY_WAREHOUSE">EPC + СКЛАД</option>
          <option value="WAREHOUSE_ONLY">ТОЛЬКО СКЛАД</option>
          <option value="NOT_FOUND">НЕ НАЙДЕНО</option>
        </select>
      </div>
      <div class="field">
        <label>Транспорт</label>
        <select id="fTransport">
          <option value="">Все</option>
          <option value="FORKLIFT">ПОГРУЗЧИК</option>
          <option value="UNKNOWN">НЕИЗВЕСТНО</option>
          <option value="ALONE">САМА</option>
          <option value="HUMAN">ЧЕЛОВЕК</option>
          <option value="WAREHOUSE">СКЛАД</option>
        </select>
      </div>
      <div class="field">
        <label>Статус допроверки</label>
        <select id="fPending">
          <option value="">Все</option>
          <option value="1">Нужна допроверка</option>
          <option value="0">Финализировано</option>
        </select>
      </div>
      <div class="field">
        <label>Лимит</label>
        <select id="fLimit">
          <option>100</option>
          <option selected>200</option>
          <option>500</option>
          <option>1000</option>
        </select>
      </div>
      <div class="field" style="grid-column: span 2;">
        <label>Поиск</label>
        <input id="fSearch" placeholder="4223/26 / Tag / EPC / TID / документ 1С / Warehouse" />
      </div>
      <div class="actions">
        <button class="btn-primary" onclick="loadAll()">Обновить</button>
      </div>
      <div class="actions">
        <button class="btn-light" onclick="clearFilters()">Сбросить</button>
      </div>
    </div>

    <div class="card table-card">
      <div class="table-head">
        <div>
          <div class="panel-title">Журнал событий</div>
          <div class="panel-desc">Сырые и финализированные события из сводной таблицы.</div>
        </div>
        <div class="hint" id="eventsMeta">—</div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Время</th>
              <th>Итог</th>
              <th>Уверенность</th>
              <th>RFID</th>
              <th>1С / серия</th>
              <th>Видео</th>
              <th>СКУД</th>
              <th>Recheck</th>
              <th>Транспорт</th>
              <th>Предупреждения</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="eventsBody"></tbody>
        </table>
      </div>
    </div>

    <div class="footer-note" id="footerInfo">—</div>
  </div>

  <div class="modal" id="modal">
    <div class="modal-card">
      <div class="modal-head">
        <div>
          <h3 class="modal-title" id="modalTitle">Событие</h3>
          <div class="modal-sub" id="modalSub">—</div>
        </div>
        <button class="btn-light" onclick="closeModal()">Закрыть</button>
      </div>
      <div class="modal-body" id="modalBody"></div>
    </div>
  </div>

  <script>
    const REFRESH_SEC = {{ refresh_sec }};

    function русНаправление(v) {
      const m = { IN: 'ВЪЕЗД', OUT: 'ВЫЕЗД', UNKNOWN: 'НЕ ОПРЕДЕЛЕНО', '0>1': 'ВЫЕЗД', '1>0': 'ВЪЕЗД' };
      return m[v] || v || '—';
    }

    function русКонсенсус(v) {
      const m = { UNANIMOUS: 'ЕДИНОГЛАСНО', SINGLE: 'ОДИН ИСТОЧНИК', MAJORITY: 'БОЛЬШИНСТВО', SPLIT: 'РАЗНОГЛАСИЕ', NO_DATA: 'НЕТ ДАННЫХ' };
      return m[v] || v || '—';
    }

    function русСопоставление(v) {
      const m = {
        FULL_TAG: 'ПОЛНАЯ МЕТКА',
        EPC_ONLY: 'ТОЛЬКО EPC',
        FULL_TAG_WAREHOUSE: 'ПОЛНАЯ + СКЛАД',
        EPC_ONLY_WAREHOUSE: 'EPC + СКЛАД',
        WAREHOUSE_ONLY: 'ТОЛЬКО СКЛАД',
        NOT_FOUND: 'НЕ НАЙДЕНО'
      };
      return m[v] || v || '—';
    }

    function русТранспорт(v) {
      const m = { FORKLIFT: 'ПОГРУЗЧИК', HUMAN: 'ЧЕЛОВЕК', ALONE: 'САМА', UNKNOWN: 'НЕИЗВЕСТНО', WAREHOUSE: 'СКЛАД', forklift: 'погрузчик', human: 'человек', alone: 'сама', 'forklift+human': 'погрузчик+человек' };
      return m[v] || v || '—';
    }

    function русПричина(v) {
      const m = { TIMEOUT: 'ТАЙМАУТ', RECHECK: 'ПОВТОРНАЯ ПРОВЕРКА', MAX_DURATION: 'МАКСИМАЛЬНАЯ ДЛИТЕЛЬНОСТЬ', WAREHOUSE_ONLY: 'ТОЛЬКО СКЛАД', RECOVERY: 'ВОССТАНОВЛЕНИЕ', FULL_REBUILD: 'ПОЛНАЯ ПЕРЕСБОРКА' };
      return m[v] || v || '—';
    }

    let timelineChart = null;
    let matchChart = null;

    function fmtDate(value) {
      if (!value) return '—';
      const d = new Date(value);
      if (Number.isNaN(d.getTime())) return value;
      return d.toLocaleString('ru-RU');
    }

    function tagClassDirection(v) {
      if (v === 'OUT') return 'tag tag-out';
      if (v === 'IN') return 'tag tag-in';
      return 'tag tag-unknown';
    }

    function tagClassPending(v) {
      return v ? 'tag tag-pending' : 'tag tag-ok';
    }

    function tagClassMatch(v) {
      if (['FULL_TAG_WAREHOUSE','EPC_ONLY_WAREHOUSE','WAREHOUSE_ONLY'].includes(v)) return 'tag tag-warehouse';
      return v === 'NOT_FOUND' ? 'tag tag-not-found' : 'tag tag-match';
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;');
    }

    async function getJson(url) {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    }

    function clearFilters() {
      document.getElementById('fDirection').value = '';
      document.getElementById('fConsensus').value = '';
      document.getElementById('fMatchType').value = '';
      document.getElementById('fTransport').value = '';
      document.getElementById('fPending').value = '';
      document.getElementById('fLimit').value = '200';
      document.getElementById('fSearch').value = '';
      loadAll();
    }

    function buildEventsUrl() {
      const params = new URLSearchParams();
      const mapping = {
        direction: document.getElementById('fDirection').value,
        consensus: document.getElementById('fConsensus').value,
        match_type: document.getElementById('fMatchType').value,
        transport: document.getElementById('fTransport').value,
        pending: document.getElementById('fPending').value,
        limit: document.getElementById('fLimit').value,
        search: document.getElementById('fSearch').value.trim(),
      };
      Object.entries(mapping).forEach(([k, v]) => { if (v) params.set(k, v); });
      return `/api/events?${params.toString()}`;
    }

    function renderMetrics(summary) {
      const cards = [
        ['Событий за окно', summary.total_events, 'Последние {{ summary_hours }} ч', 'accent'],
        ['Ждут допроверки', summary.pending_recheck, 'Ждут дообогащения', 'warn'],
        ['Финализировано', summary.finalized_events, 'Окончательно закрытые события', 'good'],
        ['Связано с 1С', summary.matched_1c, 'Номер 1С заполнен', 'accent'],
        ['Склад', summary.match_warehouse, `из них без КПП: ${summary.warehouse_only_events || 0}`, 'good'],
        ['Видео подтверждено', summary.video_matched, 'Есть связанное событие видео', 'good'],
        ['Средняя уверенность', `${summary.avg_confidence}%`, 'По выбранному окну', 'accent'],
      ];
      const el = document.getElementById('metrics');
      el.innerHTML = cards.map(([label, value, sub, cls]) => `
        <div class="card metric-card">
          <div class="metric-label">${label}</div>
          <div class="metric-value ${cls}">${value}</div>
          <div class="metric-sub">${sub}</div>
        </div>
      `).join('');
    }

    function renderWarnings(items) {
      const el = document.getElementById('warnings');
      if (!items.length) {
        el.innerHTML = `<div class="warning-item"><strong>Предупреждений нет</strong><div class="panel-desc">За выбранное окно не найдено повторяющихся предупреждений.</div></div>`;
        return;
      }
      el.innerHTML = items.map(x => `
        <div class="warning-item">
          <strong>${escapeHtml(x.warning)}</strong>
          <div class="panel-desc">Повторов: ${x.count}</div>
        </div>
      `).join('');
    }

    function renderTimeline(series) {
      const labels = series.map(x => x.bucket.slice(5));
      const dataIn = series.map(x => x.dir_in);
      const dataOut = series.map(x => x.dir_out);
      const dataUnknown = series.map(x => x.dir_unknown);
      const dataPending = series.map(x => x.pending);

      if (timelineChart) timelineChart.destroy();
      timelineChart = new Chart(document.getElementById('timelineChart'), {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: 'Въезд', data: dataIn, tension: 0.25, borderWidth: 2 },
            { label: 'Выезд', data: dataOut, tension: 0.25, borderWidth: 2 },
            { label: 'Не определено', data: dataUnknown, tension: 0.25, borderWidth: 2 },
            { label: 'Ждут ответа', data: dataPending, tension: 0.25, borderWidth: 2, borderDash: [6,4], hidden: true },
          ]
        },
        options: {
          maintainAspectRatio: false,
          responsive: true,
          plugins: { legend: { position: 'bottom' } },
          scales: { x: { ticks: { maxTicksLimit: 10 } }, y: { beginAtZero: true } }
        }
      });
    }

    function renderMatchChart(summary) {
      if (matchChart) matchChart.destroy();
      matchChart = new Chart(document.getElementById('matchChart'), {
        type: 'doughnut',
        data: {
          labels: ['ПОЛНАЯ МЕТКА', 'ТОЛЬКО EPC', 'СКЛАД'],
          datasets: [{ data: [summary.match_full_tag, summary.match_epc_only, summary.match_warehouse] }]
        },
        options: {
          maintainAspectRatio: false,
          plugins: { legend: { position: 'bottom' } }
        }
      });
    }


    function yesNo(value) { return value ? 'Да' : 'Нет'; }

    function signalBars(avgRssi) {
      const rssi = Number(avgRssi);
      let level = 0;
      if (!Number.isNaN(rssi)) {
        if (rssi >= -55) level = 5;
        else if (rssi >= -65) level = 4;
        else if (rssi >= -75) level = 3;
        else if (rssi >= -85) level = 2;
        else level = 1;
      }
      let bars = '';
      for (let i = 1; i <= 5; i++) bars += `<span class="${i <= level ? 'active' : ''}"></span>`;
      return `<span class="signal-bars" title="RSSI ${avgRssi ?? '—'}">${bars}</span>`;
    }

    function sourcePill(label, state, detail='') {
      const cls = state === 'ok' ? 'ok' : (state === 'warn' ? 'warn' : 'bad');
      const mark = state === 'ok' ? '✓' : (state === 'warn' ? '!' : '—');
      return `<span class="source-pill ${cls}" title="${escapeHtml(detail)}"><span>${mark}</span>${escapeHtml(label)}</span>`;
    }

    function managerStatusText(data) {
      if (data.SessionCloseReason === 'WAREHOUSE_ONLY') return 'НА СКЛАДЕ БЕЗ КПП';
      return русНаправление(data.FinalDirection);
    }

    function compactWarning(text) {
      if (!text) return 'Без предупреждений';
      const parts = String(text).split('|').map(x => x.trim()).filter(Boolean);
      return parts.slice(0, 2).join(' • ') + (parts.length > 2 ? ` • еще ${parts.length - 2}` : '');
    }

    function renderEvents(events) {
      const tbody = document.getElementById('eventsBody');
      if (!events.length) {
        tbody.innerHTML = `<tr><td colspan="12" class="empty">Нет событий по текущим фильтрам.</td></tr>`;
        return;
      }
      tbody.innerHTML = events.map(ev => {
        const confidence = Number(ev.ConfidencePct || 0);
        const previewTaskId = ev.Task1CId ? `#${ev.Task1CId}` : 'нет';
        const previewSeries = ev.Task1CSeriesNumber ? `Серия: ${ev.Task1CSeriesNumber}` : 'Серия: —';
        const previewVideo = ev.VideoMatched ? `${русНаправление(ev.VideoDirection)} / №${ev.VideoEventId}` : 'нет';
        const previewSkud = ev.SkudMatched ? `${русНаправление(ev.SkudDirection)} / ${ev.SkudPerson || ''}` : 'нет';
        const warehouseParts = [];
        if (ev.WarehouseId) warehouseParts.push(`Склад ID: ${ev.WarehouseId}`);
        if (ev.WarehouseSeriesNumber) warehouseParts.push(`Склад серия: ${ev.WarehouseSeriesNumber}`);
        if (ev.WarehouseDt) warehouseParts.push(`Склад: ${fmtDate(ev.WarehouseDt)}`);
        const warehouseLine = warehouseParts.length ? warehouseParts.join(' • ') : 'Склад: —';
        return `
          <tr>
            <td class="mono">${ev.EventId}</td>
            <td>
              <div><strong>${fmtDate(ev.FirstSeen)}</strong></div>
              <div class="panel-desc">до ${fmtDate(ev.LastSeen)}</div>
            </td>
            <td>
              <div class="${tagClassDirection(ev.FinalDirection)}">${ev.FinalDirection}</div>
              <div class="panel-desc" style="margin-top:6px;">${русКонсенсус(ev.ConsensusCode)}</div>
            </td>
            <td class="confidence">
              <div><strong>${confidence}%</strong></div>
              <div class="bar"><span style="width:${Math.max(0, Math.min(100, confidence))}%"></span></div>
            </td>
            <td>
              <div class="mono">reads=${ev.RfidReadCount}</div>
              <div class="panel-desc">${escapeHtml(ev.RfidZonesCsv || '—')} / ${escapeHtml(ev.RfidAntennasCsv || '—')}</div>
            </td>
            <td>
              <div class="${tagClassMatch(ev.TaskMatchType)}">${русСопоставление(ev.TaskMatchType)}</div>
              <div class="panel-desc">${previewTaskId}</div>
              <div class="panel-desc">${escapeHtml(previewSeries)}</div>
              <div class="panel-desc">${escapeHtml(warehouseLine)}</div>
            </td>
            <td>
              <div>${previewVideo}</div>
              <div class="panel-desc">${русТранспорт(ev.VideoTransport)}</div>
            </td>
            <td>
              <div>${previewSkud}</div>
            </td>
            <td>
              <div class="${tagClassPending(ev.NeedRecheck)}">${ev.NeedRecheck ? 'ожидает' : 'готово'}</div>
              <div class="panel-desc">x${ev.RecheckCount || 0}</div>
            </td>
            <td>${русТранспорт(ev.TransportMode)}</td>
            <td>${escapeHtml(ev.WarningFlags || '—')}</td>
            <td><button class="link-btn" onclick="openEvent(${ev.EventId})">Подробнее</button></td>
          </tr>
        `;
      }).join('');
      document.getElementById('eventsMeta').textContent = `Показано событий: ${events.length}`;
    }

    async function openEvent(eventId) {
      const data = await getJson(`/api/event/${eventId}`);
      document.getElementById('modalTitle').textContent = `Событие №${data.EventId} • ${managerStatusText(data)}`;
      document.getElementById('modalSub').textContent = `Обновлено: ${fmtDate(data.UpdatedAt)} • Ключ: ${data.EventKey}`;

      const evidence = JSON.stringify(data.EvidenceJsonParsed ?? data.EvidenceJson ?? {}, null, 2);
      const imageBlock = data.VideoEventId
        ? `<div class="img-box image-large"><img src="/api/image/${data.VideoEventId}" alt="Снимок события" onerror="this.parentElement.innerHTML='Снимок недоступен'" /></div>`
        : `<div class="img-box image-large">Снимок недоступен</div>`;

      const warehouseFound = Boolean(data.WarehouseId);
      const oneCFound = Boolean(data.Task1CId);
      const statusText = managerStatusText(data);
      const signal = signalBars(data.AvgRSSI);
      const sourceStrip = [
        sourcePill('RFID', data.RfidReadCount ? 'ok' : 'bad', `${data.RfidReadCount || 0} считываний`),
        sourcePill('Видео', data.VideoMatched ? 'ok' : 'bad', data.VideoMatched ? `Видео №${data.VideoEventId}` : 'Нет видео'),
        sourcePill('СКУД', data.SkudMatched ? 'ok' : 'bad', data.SkudMatched ? data.SkudPerson || 'Подтвержден' : 'Нет СКУД'),
        sourcePill('1С', oneCFound ? 'ok' : (data.NeedRecheck ? 'warn' : 'bad'), oneCFound ? `№${data.Task1CId}` : 'Не найдено'),
        sourcePill('Склад', warehouseFound ? 'ok' : 'bad', warehouseFound ? `${data.WarehouseSeriesNumber || data.WarehouseId}` : 'Не найден')
      ].join('');

      document.getElementById('modalBody').innerHTML = `
        <div class="manager-view">
          <div class="manager-hero">
            <div class="manager-decision">${escapeHtml(statusText)}</div>
            <div class="panel-desc">Главная карточка для диспетчера / менеджера</div>
            <div class="source-strip">${sourceStrip}</div>
            <div class="manager-row">
              <div class="manager-kpi"><small>Уверенность решения</small><strong>${data.ConfidencePct || 0}%</strong><div class="bar"><span style="width:${Math.max(0, Math.min(100, Number(data.ConfidencePct || 0)))}%"></span></div></div>
              <div class="manager-kpi"><small>Сигнал RFID</small><strong>${signal}${data.AvgRSSI ?? '—'} RSSI</strong></div>
              <div class="manager-kpi"><small>Катушка / серия</small><strong>${escapeHtml(data.WarehouseSeriesNumber || data.Task1CSeriesNumber || '—')}</strong></div>
              <div class="manager-kpi"><small>Склад</small><strong>${warehouseFound ? 'Зафиксирована' : 'Нет отметки'}</strong><div class="panel-desc">${warehouseFound ? fmtDate(data.WarehouseDt) : '—'}</div></div>
              <div class="manager-kpi"><small>Документ 1С</small><strong>${escapeHtml(data.Task1CDocIds || data.WarehouseDocIds || '—')}</strong></div>
              <div class="manager-kpi"><small>Предупреждения</small><strong>${escapeHtml(compactWarning(data.WarningFlags))}</strong></div>
            </div>
          </div>
          <div class="detail-card">
            <div class="detail-title">Фото / видео события</div>
            ${imageBlock}
          </div>
        </div>

        <div class="expert-section">
          <details class="expert-block" open>
            <summary>Источники данных</summary>
            <div class="expert-content">
              <div class="detail-grid">
                <div class="detail-item"><small>Первое появление</small><span>${fmtDate(data.FirstSeen)}</span></div>
                <div class="detail-item"><small>Последнее появление</small><span>${fmtDate(data.LastSeen)}</span></div>
                <div class="detail-item"><small>Итоговое направление</small><span>${русНаправление(data.FinalDirection)}</span></div>
                <div class="detail-item"><small>Консенсус</small><span>${русКонсенсус(data.ConsensusCode)}</span></div>
                <div class="detail-item"><small>Транспорт</small><span>${русТранспорт(data.TransportMode)}</span></div>
                <div class="detail-item"><small>Нужна допроверка</small><span>${yesNo(data.NeedRecheck)}</span></div>
                <div class="detail-item"><small>Видео</small><span>${data.VideoMatched ? `Да, №${data.VideoEventId} / ${fmtDate(data.VideoTime)}` : 'Нет'}</span></div>
                <div class="detail-item"><small>СКУД</small><span>${data.SkudMatched ? `${escapeHtml(data.SkudPerson || 'Да')} / ${fmtDate(data.SkudTime)}` : 'Нет'}</span></div>
                <div class="detail-item"><small>1С</small><span>${oneCFound ? `№${data.Task1CId} / ${escapeHtml(data.Task1CSeriesNumber || '—')}` : 'Не найдено'}</span></div>
                <div class="detail-item"><small>Warehouse</small><span>${warehouseFound ? `ID ${data.WarehouseId} / ${escapeHtml(data.WarehouseSeriesNumber || '—')}` : 'Нет'}</span></div>
              </div>
            </div>
          </details>

          <details class="expert-block">
            <summary>RFID подробно</summary>
            <div class="expert-content">
              <div class="detail-grid">
                <div class="detail-item"><small>EPC</small><span class="mono">${escapeHtml(data.EPC || '')}</span></div>
                <div class="detail-item"><small>TID</small><span class="mono">${escapeHtml(data.TID || '')}</span></div>
                <div class="detail-item"><small>Число считываний</small><span>${data.RfidReadCount ?? '—'}</span></div>
                <div class="detail-item"><small>Антенны</small><span>${escapeHtml(data.RfidAntennasCsv || '—')}</span></div>
                <div class="detail-item"><small>Зоны</small><span>${escapeHtml(data.RfidZonesCsv || '—')}</span></div>
                <div class="detail-item"><small>Первая/последняя зона</small><span>${escapeHtml(data.FirstZone || '—')} → ${escapeHtml(data.LastZone || '—')}</span></div>
                <div class="detail-item"><small>Направление RFID</small><span>${escapeHtml(русНаправление(data.RfidDirection))} / баллы ${data.RfidDirectionScore || 0}</span></div>
                <div class="detail-item"><small>RSSI ср/мин/макс</small><span>${signal}${data.AvgRSSI ?? '—'} / ${data.MinRSSI ?? '—'} / ${data.MaxRSSI ?? '—'}</span></div>
              </div>
            </div>
          </details>

          <details class="expert-block">
            <summary>Видео, СКУД, 1С и склад подробно</summary>
            <div class="expert-content">
              <div class="detail-grid">
                <div class="detail-item"><small>Видео направление</small><span>${escapeHtml(русНаправление(data.VideoDirection))}</span></div>
                <div class="detail-item"><small>Видео транспорт</small><span>${escapeHtml(русТранспорт(data.VideoTransport))}</span></div>
                <div class="detail-item"><small>Видео баллы / Δt</small><span>${data.VideoScore || 0} / ${data.VideoTimeDeltaMs ?? '—'} ms</span></div>
                <div class="detail-item"><small>СКУД направление</small><span>${escapeHtml(русНаправление(data.SkudDirection))}</span></div>
                <div class="detail-item"><small>СКУД точка</small><span>${escapeHtml(data.SkudGate || '—')}</span></div>
                <div class="detail-item"><small>СКУД карта</small><span>${escapeHtml(data.SkudCard || '—')}</span></div>
                <div class="detail-item"><small>Тип сопоставления 1С</small><span>${escapeHtml(русСопоставление(data.TaskMatchType))}</span></div>
                <div class="detail-item"><small>Время 1С</small><span>${fmtDate(data.Task1CDt)}</span></div>
                <div class="detail-item"><small>Документ склада</small><span>${escapeHtml(data.WarehouseDocIds || '—')}</span></div>
                <div class="detail-item"><small>Время склада</small><span>${fmtDate(data.WarehouseDt)}</span></div>
                <div class="detail-item"><small>Причина закрытия</small><span>${escapeHtml(русПричина(data.SessionCloseReason))}</span></div>
                <div class="detail-item"><small>Проверки</small><span>${data.RecheckCount || 0}; следующая ${fmtDate(data.NextRecheckAt)}</span></div>
              </div>
            </div>
          </details>

          <details class="expert-block json-toggle">
            <summary>Подробности решения JSON</summary>
            <div class="expert-content"><pre>${escapeHtml(evidence)}</pre></div>
          </details>
        </div>
      `;
      document.getElementById('modal').classList.add('open');
    }

    function closeModal() {
      document.getElementById('modal').classList.remove('open');
    }

    async function loadAll() {
      const [runtime, summary, charts, warnings, events] = await Promise.all([
        getJson('/api/runtime'),
        getJson('/api/summary'),
        getJson('/api/charts'),
        getJson('/api/warnings'),
        getJson(buildEventsUrl()),
      ]);

      const lastRfid = runtime.LAST_RFID_ID || {};
      document.getElementById('lastRfidId').textContent = lastRfid.value ?? '—';
      document.getElementById('lastRfidUpdated').textContent = lastRfid.updated_at ? `Обновлено: ${fmtDate(lastRfid.updated_at)}` : 'Состояние не найдено';
      document.getElementById('serverTime').textContent = `Сервер: ${fmtDate(summary.server_time)}`;
      document.getElementById('footerInfo').textContent = `Окно сводки: последние {{ summary_hours }} ч • График: {{ chart_days }} д • Последнее обновление: ${fmtDate(summary.server_time)}`;

      renderMetrics(summary);
      renderWarnings(warnings.items);
      renderTimeline(charts.series);
      renderMatchChart(summary);
      renderEvents(events.items);
    }

    document.getElementById('fSearch').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') loadAll();
    });

    document.getElementById('modal').addEventListener('click', (e) => {
      if (e.target.id === 'modal') closeModal();
    });

    loadAll();
    setInterval(loadAll, REFRESH_SEC * 1000);
  </script>
</body>
</html>
"""


@app.route("/")
def index() -> str:
    return render_template_string(
        PAGE,
        refresh_sec=Config.AUTO_REFRESH_SEC,
        summary_hours=Config.SUMMARY_HOURS,
        chart_days=Config.CHART_DAYS,
    )


@app.route("/api/runtime")
def api_runtime() -> Response:
    return Response(json.dumps(fetch_runtime_state(), cls=JsonEncoder), mimetype="application/json")


@app.route("/api/summary")
def api_summary() -> Response:
    data = fetch_summary(Config.SUMMARY_HOURS)
    data["server_time"] = datetime.now().isoformat()
    return Response(json.dumps(data, cls=JsonEncoder), mimetype="application/json")


@app.route("/api/charts")
def api_charts() -> Response:
    return Response(json.dumps(fetch_chart_series(Config.CHART_DAYS), cls=JsonEncoder), mimetype="application/json")


@app.route("/api/warnings")
def api_warnings() -> Response:
    return Response(
        json.dumps({"items": fetch_top_warnings(Config.SUMMARY_HOURS)}, cls=JsonEncoder),
        mimetype="application/json",
    )


@app.route("/api/events")
def api_events() -> Response:
    limit = int(request.args.get("limit", Config.DEFAULT_LIMIT))
    items = fetch_events(
        limit=limit,
        direction=request.args.get("direction", "").strip(),
        consensus=request.args.get("consensus", "").strip(),
        match_type=request.args.get("match_type", "").strip(),
        transport=request.args.get("transport", "").strip(),
        pending=request.args.get("pending", "").strip(),
        search=request.args.get("search", "").strip(),
    )
    return Response(json.dumps({"items": items}, cls=JsonEncoder), mimetype="application/json")


@app.route("/api/event/<int:event_id>")
def api_event(event_id: int) -> Response:
    item = fetch_event_details(event_id)
    if not item:
        return Response(json.dumps({"ошибка": "не найдено"}), status=404, mimetype="application/json")
    return Response(json.dumps(item, cls=JsonEncoder), mimetype="application/json")


@app.route("/api/image/<int:video_event_id>")
def api_image(video_event_id: int) -> Response:
    query = """
    SELECT TOP 1 ImageBase64, ImageFormat
    FROM dbo.ReelTransitions
    WHERE Id = ?
    """
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(query, video_event_id)
        row = cur.fetchone()
        if not row:
            return Response(b"", status=404)
        img_data = row[0]
        img_format = str(row[1] or "jpg").lower()
        if not img_data:
            return Response(b"", status=404)
        mime = "image/jpeg" if img_format in {"jpg", "jpeg"} else f"image/{img_format}"
        return Response(img_data, mimetype=mime)


@app.route("/favicon.ico")
def favicon() -> Response:
    return Response(status=204)


if __name__ == "__main__":
    print("\n" + "█" * 100)
    print("🌐 КПП • WEB v2.4 по KPP_ReelEvents")
    print("█" * 100)
    print(f"Адрес: {Config.HOST}")
    print(f"Порт: {Config.PORT}")
    print(f"Окно сводки: {Config.SUMMARY_HOURS} ч")
    print(f"Окно графика: {Config.CHART_DAYS} д")
    print(f"Обновление: {Config.AUTO_REFRESH_SEC} с")
    print("█" * 100)
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG, use_reloader=False)
