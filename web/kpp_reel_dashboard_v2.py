#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Веб-дашборд для мониторинга КПП по сводной таблице KPP_ReelEvents.

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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pyodbc
from flask import Flask, Response, jsonify, render_template_string, request


# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================


class Config:
    HOST = os.getenv("KPP_WEB_HOST", "127.0.0.1")
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
            # Preserve integers as int and fractional values as float for JSON
            return int(obj) if obj == obj.to_integral_value() else float(obj)
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
        SUM(CASE WHEN TaskMatchType = 'NOT_FOUND' THEN 1 ELSE 0 END) AS MatchNotFound
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
    SELECT TOP 10 WarningFlags, COUNT(*) AS Cnt
    FROM dbo.KPP_ReelEvents
    WHERE FirstSeen >= DATEADD(hour, -?, GETDATE())
      AND ISNULL(WarningFlags, '') <> ''
    GROUP BY WarningFlags
    ORDER BY COUNT(*) DESC, WarningFlags ASC
    """
    result: List[Dict[str, Any]] = []
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(query, hours)
        for warning_flags, cnt in cur.fetchall():
            result.append({"warning": str(warning_flags), "count": int(cnt or 0)})
    return result


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
            "ISNULL(EventKey, '') LIKE ? OR "
            "ISNULL(SourceTag, '') LIKE ? OR "
            "ISNULL(EPC, '') LIKE ? OR "
            "ISNULL(TID, '') LIKE ? OR "
            "ISNULL(Task1CDocIds, '') LIKE ? OR "
            "CAST(ISNULL(Task1CId, '') AS nvarchar(50)) LIKE ?"
            ")"
        )
        like = f"%{search}%"
        params.extend([like, like, like, like, like, like])

    query = f"""
    SELECT TOP (?)
        EventId,
        EventKey,
        SourceTag,
        EPC,
        TID,
        Task1CId,
        Task1CDt,
        Task1CDocIds,
        TaskMatchType,
        FirstSeen,
        LastSeen,
        CompletedAt,
        SessionCloseReason,
        RfidReadCount,
        DistinctAntennaCount,
        DistinctZoneCount,
        RfidFirstAntenna,
        RfidLastAntenna,
        FirstZone,
        LastZone,
        RfidAntennasCsv,
        RfidZonesCsv,
        AvgRSSI,
        MinRSSI,
        MaxRSSI,
        DurationMs,
        RfidDirection,
        RfidDirectionScore,
        VideoMatched,
        VideoEventId,
        VideoTime,
        VideoDirection,
        VideoTransport,
        VideoTimeDeltaMs,
        VideoScore,
        SkudMatched,
        SkudExternalId,
        SkudTime,
        SkudDirection,
        SkudGate,
        SkudPerson,
        SkudCard,
        SkudTimeDeltaMs,
        SkudScore,
        FinalDirection,
        ConfidencePct,
        ConsensusCode,
        ScoreIn,
        ScoreOut,
        SourceCount,
        TransportMode,
        WarningFlags,
        EvidenceJson,
        NeedRecheck,
        NextRecheckAt,
        RecheckCount,
        LastRecheckAt,
        CreatedAt,
        UpdatedAt,
        FinalizedAt
    FROM dbo.KPP_ReelEvents
    WHERE {' AND '.join(where)}
    ORDER BY COALESCE(FinalizedAt, UpdatedAt, CompletedAt, LastSeen, FirstSeen) DESC, EventId DESC
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
    query = """
    SELECT *
    FROM dbo.KPP_ReelEvents
    WHERE EventId = ?
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
  <title>КПП • Сводный мониторинг</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      --bg: #f5f7fb;
      --surface: #ffffff;
      --surface-2: #f9fbff;
      --border: #e6ebf3;
      --text: #19212e;
      --muted: #667286;
      --accent: #2f6bff;
      --accent-soft: #ebf2ff;
      --good: #16a34a;
      --good-soft: #e9f9ef;
      --warn: #d97706;
      --warn-soft: #fff3e6;
      --bad: #dc2626;
      --bad-soft: #fdecec;
      --shadow: 0 10px 30px rgba(27, 39, 59, 0.08);
      --radius: 18px;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      background: linear-gradient(180deg, #f7f9fc 0%, #eef3f9 100%);
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
      background: var(--surface);
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
    }
    .subtitle {
      color: var(--muted);
      font-size: 14px;
      line-height: 1.5;
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
      border-radius: 14px;
      background: var(--surface-2);
      border: 1px solid var(--border);
    }
    .warning-item strong,
    .logic-item strong {
      display: block;
      margin-bottom: 6px;
    }
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
      border-radius: 12px;
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
      border-radius: 12px;
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
      width: min(1360px, 100%);
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
      grid-template-columns: 1.15fr 1fr;
      gap: 18px;
    }
    .detail-card {
      border: 1px solid var(--border);
      border-radius: 18px;
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
      border-radius: 18px;
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
    pre {
      background: #0f172a;
      color: #e5edf8;
      padding: 16px;
      border-radius: 14px;
      overflow: auto;
      font-size: 12px;
      line-height: 1.5;
      margin: 0;
    }
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
        <h1 class="title">КПП • Сводный мониторинг катушек</h1>
        <div class="subtitle">
          Дашборд поверх <span class="mono">KPP_ReelEvents</span>: показывает итоговые события,
          pending-переобогащение, матчинг с 1С, подтверждения от RFID / видео / СКУД и финальное решение по направлению.
        </div>
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
            <div class="panel-desc">Въезды, выезды, unknown и pending по 30-минутным интервалам.</div>
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
              <div class="panel-desc">Насколько событие удалось связать с документом 1С.</div>
            </div>
          </div>
          <div class="chart-wrap" style="height: 250px;"><canvas id="matchChart"></canvas></div>
        </div>
        <div class="card panel">
          <div class="panel-header">
            <div>
              <h2 class="panel-title">Частые предупреждения</h2>
              <div class="panel-desc">Что чаще всего мешает финализации.</div>
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
          <div class="logic-list">
            <div class="logic-item"><strong>1. RFID-сессия</strong> Считывания одной и той же метки группируются в сессию по времени пропадания.</div>
            <div class="logic-item"><strong>2. Корреляция</strong> К сессии подбираются ближайшие события видео и СКУД в заданных окнах времени.</div>
            <div class="logic-item"><strong>3. Матчинг 1С</strong> Событие связывается с 1С по FULL_TAG или EPC_ONLY; если данных пока нет, событие остается на повторной проверке.</div>
            <div class="logic-item"><strong>4. Финализация</strong> При достаточном консенсусе и/или сильном RFID направлении событие получает итог и перестает ждать переобогащения.</div>
          </div>
        </div>
      </div>
    </div>

    <div class="card filters">
      <div class="field">
        <label>Направление</label>
        <select id="fDirection">
          <option value="">Все</option>
          <option value="IN">IN</option>
          <option value="OUT">OUT</option>
          <option value="UNKNOWN">UNKNOWN</option>
        </select>
      </div>
      <div class="field">
        <label>Консенсус</label>
        <select id="fConsensus">
          <option value="">Все</option>
          <option value="UNANIMOUS">UNANIMOUS</option>
          <option value="SINGLE">SINGLE</option>
          <option value="NO_DATA">NO_DATA</option>
          <option value="SPLIT">SPLIT</option>
        </select>
      </div>
      <div class="field">
        <label>Матчинг 1С</label>
        <select id="fMatchType">
          <option value="">Все</option>
          <option value="FULL_TAG">FULL_TAG</option>
          <option value="EPC_ONLY">EPC_ONLY</option>
          <option value="NOT_FOUND">NOT_FOUND</option>
        </select>
      </div>
      <div class="field">
        <label>Транспорт</label>
        <select id="fTransport">
          <option value="">Все</option>
          <option value="FORKLIFT">FORKLIFT</option>
          <option value="UNKNOWN">UNKNOWN</option>
          <option value="ALONE">ALONE</option>
          <option value="HUMAN">HUMAN</option>
        </select>
      </div>
      <div class="field">
        <label>Pending / finalized</label>
        <select id="fPending">
          <option value="">Все</option>
          <option value="1">NeedRecheck = 1</option>
          <option value="0">NeedRecheck = 0</option>
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
        <input id="fSearch" placeholder="EventKey / EPC / TID / 1C ID / документ" />
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
          <div class="panel-desc">Сырые и финализированные события из KPP_ReelEvents.</div>
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
              <th>1С</th>
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
        ['Pending recheck', summary.pending_recheck, 'Ждут дообогащения', 'warn'],
        ['Финализировано', summary.finalized_events, 'Закрытые события', 'good'],
        ['Связано с 1С', summary.matched_1c, 'Task1CId заполнен', 'accent'],
        ['Видео подтверждено', summary.video_matched, 'Есть VideoEventId', 'good'],
        ['Средняя уверенность', `${summary.avg_confidence}%`, 'По окну summary', 'accent'],
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
        el.innerHTML = `<div class="warning-item"><strong>Предупреждений нет</strong><div class="panel-desc">За выбранное окно не найдено частых warning flags.</div></div>`;
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
            { label: 'IN', data: dataIn, tension: 0.25, borderWidth: 2 },
            { label: 'OUT', data: dataOut, tension: 0.25, borderWidth: 2 },
            { label: 'UNKNOWN', data: dataUnknown, tension: 0.25, borderWidth: 2 },
            { label: 'Pending', data: dataPending, tension: 0.25, borderWidth: 2, borderDash: [6,4] },
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
          labels: ['FULL_TAG', 'EPC_ONLY', 'NOT_FOUND'],
          datasets: [{ data: [summary.match_full_tag, summary.match_epc_only, summary.match_not_found] }]
        },
        options: {
          maintainAspectRatio: false,
          plugins: { legend: { position: 'bottom' } }
        }
      });
    }

    function renderEvents(events) {
      const tbody = document.getElementById('eventsBody');
      if (!events.length) {
        tbody.innerHTML = `<tr><td colspan="12" class="empty">Нет событий по текущим фильтрам.</td></tr>`;
        return;
      }
      tbody.innerHTML = events.map(ev => {
        const confidence = Number(ev.ConfidencePct || 0);
        const previewTask = ev.Task1CId ? `#${ev.Task1CId}` : 'нет';
        const previewVideo = ev.VideoMatched ? `${ev.VideoDirection || '—'} / #${ev.VideoEventId}` : 'нет';
        const previewSkud = ev.SkudMatched ? `${ev.SkudDirection || '—'} / ${ev.SkudPerson || ''}` : 'нет';
        return `
          <tr>
            <td class="mono">${ev.EventId}</td>
            <td>
              <div><strong>${fmtDate(ev.FirstSeen)}</strong></div>
              <div class="panel-desc">до ${fmtDate(ev.LastSeen)}</div>
            </td>
            <td>
              <div class="${tagClassDirection(ev.FinalDirection)}">${ev.FinalDirection}</div>
              <div class="panel-desc" style="margin-top:6px;">${ev.ConsensusCode || '—'}</div>
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
              <div class="${tagClassMatch(ev.TaskMatchType)}">${ev.TaskMatchType || '—'}</div>
              <div class="panel-desc">${previewTask}</div>
            </td>
            <td>
              <div>${previewVideo}</div>
              <div class="panel-desc">${ev.VideoTransport || '—'}</div>
            </td>
            <td>
              <div>${previewSkud}</div>
            </td>
            <td>
              <div class="${tagClassPending(ev.NeedRecheck)}">${ev.NeedRecheck ? 'pending' : 'done'}</div>
              <div class="panel-desc">x${ev.RecheckCount || 0}</div>
            </td>
            <td>${ev.TransportMode || '—'}</td>
            <td>${escapeHtml(ev.WarningFlags || '—')}</td>
            <td><button class="link-btn" onclick="openEvent(${ev.EventId})">Подробнее</button></td>
          </tr>
        `;
      }).join('');
      document.getElementById('eventsMeta').textContent = `Показано: ${events.length}`;
    }

    async function openEvent(eventId) {
      const data = await getJson(`/api/event/${eventId}`);
      document.getElementById('modalTitle').textContent = `Событие #${data.EventId} • ${data.FinalDirection}`;
      document.getElementById('modalSub').textContent = `EventKey: ${data.EventKey} • Updated: ${fmtDate(data.UpdatedAt)}`;

      const evidence = JSON.stringify(data.EvidenceJsonParsed ?? data.EvidenceJson ?? {}, null, 2);
      const imageBlock = data.VideoEventId
        ? `<div class="img-box"><img src="/api/image/${data.VideoEventId}" alt="Видео-превью" onerror="this.parentElement.innerHTML='Фото недоступно'" /></div>`
        : `<div class="img-box">Фото недоступно</div>`;

      document.getElementById('modalBody').innerHTML = `
        <div>
          <div class="detail-card" style="margin-bottom:16px;">
            <div class="detail-title">Сводка</div>
            <div class="detail-grid">
              <div class="detail-item"><small>FirstSeen</small><span>${fmtDate(data.FirstSeen)}</span></div>
              <div class="detail-item"><small>LastSeen</small><span>${fmtDate(data.LastSeen)}</span></div>
              <div class="detail-item"><small>CompletedAt</small><span>${fmtDate(data.CompletedAt)}</span></div>
              <div class="detail-item"><small>FinalizedAt</small><span>${fmtDate(data.FinalizedAt)}</span></div>
              <div class="detail-item"><small>FinalDirection</small><span>${data.FinalDirection || '—'}</span></div>
              <div class="detail-item"><small>Consensus</small><span>${data.ConsensusCode || '—'}</span></div>
              <div class="detail-item"><small>Confidence</small><span>${data.ConfidencePct || 0}%</span></div>
              <div class="detail-item"><small>NeedRecheck</small><span>${data.NeedRecheck ? 'Да' : 'Нет'}</span></div>
              <div class="detail-item"><small>TransportMode</small><span>${data.TransportMode || '—'}</span></div>
              <div class="detail-item"><small>WarningFlags</small><span>${escapeHtml(data.WarningFlags || '—')}</span></div>
            </div>
          </div>
          <div class="detail-card" style="margin-bottom:16px;">
            <div class="detail-title">RFID</div>
            <div class="detail-grid">
              <div class="detail-item"><small>EPC</small><span class="mono">${escapeHtml(data.EPC || '')}</span></div>
              <div class="detail-item"><small>TID</small><span class="mono">${escapeHtml(data.TID || '')}</span></div>
              <div class="detail-item"><small>ReadCount</small><span>${data.RfidReadCount ?? '—'}</span></div>
              <div class="detail-item"><small>Distinct zones</small><span>${data.DistinctZoneCount ?? '—'}</span></div>
              <div class="detail-item"><small>Zones CSV</small><span>${escapeHtml(data.RfidZonesCsv || '—')}</span></div>
              <div class="detail-item"><small>Antennas CSV</small><span>${escapeHtml(data.RfidAntennasCsv || '—')}</span></div>
              <div class="detail-item"><small>First/Last zone</small><span>${escapeHtml(data.FirstZone || '—')} → ${escapeHtml(data.LastZone || '—')}</span></div>
              <div class="detail-item"><small>RFID direction</small><span>${escapeHtml(data.RfidDirection || '—')} (score ${data.RfidDirectionScore || 0})</span></div>
              <div class="detail-item"><small>RSSI avg/min/max</small><span>${data.AvgRSSI ?? '—'} / ${data.MinRSSI ?? '—'} / ${data.MaxRSSI ?? '—'}</span></div>
              <div class="detail-item"><small>Duration</small><span>${data.DurationMs ?? '—'} ms</span></div>
            </div>
          </div>
          <div class="detail-card">
            <div class="detail-title">EvidenceJson</div>
            <pre>${escapeHtml(evidence)}</pre>
          </div>
        </div>
        <div>
          <div class="detail-card" style="margin-bottom:16px;">
            <div class="detail-title">Видео / фото</div>
            ${imageBlock}
            <div class="detail-grid" style="margin-top:12px;">
              <div class="detail-item"><small>VideoMatched</small><span>${data.VideoMatched ? 'Да' : 'Нет'}</span></div>
              <div class="detail-item"><small>VideoEventId</small><span>${data.VideoEventId || '—'}</span></div>
              <div class="detail-item"><small>VideoTime</small><span>${fmtDate(data.VideoTime)}</span></div>
              <div class="detail-item"><small>VideoDirection</small><span>${escapeHtml(data.VideoDirection || '—')}</span></div>
              <div class="detail-item"><small>VideoTransport</small><span>${escapeHtml(data.VideoTransport || '—')}</span></div>
              <div class="detail-item"><small>VideoScore / Δt</small><span>${data.VideoScore || 0} / ${data.VideoTimeDeltaMs ?? '—'} ms</span></div>
            </div>
          </div>
          <div class="detail-card" style="margin-bottom:16px;">
            <div class="detail-title">СКУД</div>
            <div class="detail-grid">
              <div class="detail-item"><small>SkudMatched</small><span>${data.SkudMatched ? 'Да' : 'Нет'}</span></div>
              <div class="detail-item"><small>SkudExternalId</small><span>${data.SkudExternalId || '—'}</span></div>
              <div class="detail-item"><small>SkudTime</small><span>${fmtDate(data.SkudTime)}</span></div>
              <div class="detail-item"><small>SkudDirection</small><span>${escapeHtml(data.SkudDirection || '—')}</span></div>
              <div class="detail-item"><small>SkudGate</small><span>${escapeHtml(data.SkudGate || '—')}</span></div>
              <div class="detail-item"><small>SkudPerson</small><span>${escapeHtml(data.SkudPerson || '—')}</span></div>
              <div class="detail-item"><small>SkudCard</small><span>${escapeHtml(data.SkudCard || '—')}</span></div>
              <div class="detail-item"><small>SkudScore / Δt</small><span>${data.SkudScore || 0} / ${data.SkudTimeDeltaMs ?? '—'} ms</span></div>
            </div>
          </div>
          <div class="detail-card">
            <div class="detail-title">1С / recheck</div>
            <div class="detail-grid">
              <div class="detail-item"><small>Task1CId</small><span>${data.Task1CId || '—'}</span></div>
              <div class="detail-item"><small>TaskMatchType</small><span>${escapeHtml(data.TaskMatchType || '—')}</span></div>
              <div class="detail-item"><small>Task1CDt</small><span>${fmtDate(data.Task1CDt)}</span></div>
              <div class="detail-item"><small>Task1CDocIds</small><span>${escapeHtml(data.Task1CDocIds || '—')}</span></div>
              <div class="detail-item"><small>NextRecheckAt</small><span>${fmtDate(data.NextRecheckAt)}</span></div>
              <div class="detail-item"><small>RecheckCount</small><span>${data.RecheckCount || 0}</span></div>
              <div class="detail-item"><small>LastRecheckAt</small><span>${fmtDate(data.LastRecheckAt)}</span></div>
              <div class="detail-item"><small>SessionCloseReason</small><span>${escapeHtml(data.SessionCloseReason || '—')}</span></div>
            </div>
          </div>
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
        return Response(json.dumps({"error": "not found"}), status=404, mimetype="application/json")
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


if __name__ == "__main__":
    print("\n" + "█" * 100)
    print("🌐 КПП • WEB DASHBOARD по KPP_ReelEvents")
    print("█" * 100)
    print(f"Host: {Config.HOST}")
    print(f"Port: {Config.PORT}")
    print(f"Summary window: {Config.SUMMARY_HOURS}h")
    print(f"Chart window: {Config.CHART_DAYS}d")
    print(f"Refresh: {Config.AUTO_REFRESH_SEC}s")
    print("█" * 100)
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG, use_reloader=False)
