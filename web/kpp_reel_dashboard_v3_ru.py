#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
КПП • WEB v3.2: промышленный мониторинг движения катушек.

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

import io
import logging
import threading
import time
import base64
import hmac
import json
import os
import re
import zipfile
import urllib.request
from html import escape as html_escape
from xml.sax.saxutils import escape as xml_escape
from decimal import Decimal
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional

import sys
from pathlib import Path
import pyodbc
from flask import Flask, Response, jsonify, render_template_string, request, g

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from common.single_instance import SingleInstanceLock  # noqa: E402


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

    DB_CONN_STR = os.getenv("KPP_WEB_DB_CONNECTION", os.getenv("RFID_DB_CONNECTION", ""))
    TASK_TABLE = os.getenv("KPP_TASK_TABLE", "dbo.RfidTags")
    REPORT_PLACE = os.getenv("KPP_REPORT_PLACE", "ЗМК Южные ворота")
    # По умолчанию данные событий никуда не отправляются. Включать явно.
    AI_BASE_URL = os.getenv("KPP_AI_BASE_URL", os.getenv("LM_STUDIO_BASE_URL", "")).rstrip("/")
    AI_FALLBACK_BASE_URL = os.getenv("KPP_AI_FALLBACK_BASE_URL", "").rstrip("/")
    AI_MODEL = os.getenv("KPP_AI_MODEL", "openai/gpt-oss-20b")
    AI_TIMEOUT_SEC = int(os.getenv("KPP_AI_TIMEOUT_SEC", "45"))
    AUTH_REQUIRED = os.getenv("KPP_WEB_AUTH_REQUIRED", "1") == "1"
    AUTH_USER = os.getenv("KPP_WEB_AUTH_USER", "")
    AUTH_PASSWORD = os.getenv("KPP_WEB_AUTH_PASSWORD", "")
    LOCK_PATH = os.getenv("KPP_WEB_LOCK_FILE", str(ROOT / "runtime" / "web_v3.lock"))
    THREADS = int(os.getenv("KPP_WEB_THREADS", "24"))
    HEARTBEAT_SEC = float(os.getenv("KPP_WEB_HEARTBEAT_SEC", "15"))
    SLOW_REQUEST_SEC = float(os.getenv("KPP_WEB_SLOW_REQUEST_SEC", "2"))


app = Flask(__name__)

log = logging.getLogger("kpp-web-v3.4")
logging.basicConfig(
    level=getattr(logging, os.getenv("KPP_WEB_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_metrics_lock = threading.Lock()
_metrics = {"requests": 0, "errors": 0, "slow": 0, "last_path": "-", "last_ms": 0.0}
_cache_lock = threading.Lock()
_cache: Dict[str, tuple] = {}


def cached_json(key: str, ttl_sec: float, builder) -> Response:
    now = time.monotonic()
    with _cache_lock:
        item = _cache.get(key)
        if item and now - item[0] <= ttl_sec:
            return Response(item[1], mimetype="application/json", headers={"X-KPP-Cache": "HIT"})
    payload = json.dumps(builder(), cls=JsonEncoder)
    with _cache_lock:
        _cache[key] = (now, payload)
    return Response(payload, mimetype="application/json", headers={"X-KPP-Cache": "MISS"})


@app.before_request
def request_metrics_start():
    g.kpp_started = time.monotonic()


@app.after_request
def request_metrics_end(response):
    elapsed = time.monotonic() - getattr(g, "kpp_started", time.monotonic())
    with _metrics_lock:
        _metrics["requests"] += 1
        _metrics["errors"] += int(response.status_code >= 500)
        _metrics["slow"] += int(elapsed >= Config.SLOW_REQUEST_SEC)
        _metrics["last_path"] = request.path
        _metrics["last_ms"] = elapsed * 1000.0
    if elapsed >= Config.SLOW_REQUEST_SEC:
        log.warning("SLOW request path=%s status=%s time=%.2fs", request.path, response.status_code, elapsed)
    return response


def web_heartbeat() -> None:
    while True:
        time.sleep(Config.HEARTBEAT_SEC)
        try:
            started = time.monotonic()
            with db_connect() as conn:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
            db_ms = (time.monotonic() - started) * 1000.0
            db_state = f"OK {db_ms:.0f}ms"
        except Exception as exc:
            db_state = f"ERROR {exc}"
        with _metrics_lock:
            snapshot = dict(_metrics)
        log.info(
            "STATUS db=%s requests=%s errors=%s slow=%s last=%s %.0fms threads=%s refresh=%ss",
            db_state, snapshot["requests"], snapshot["errors"], snapshot["slow"],
            snapshot["last_path"], snapshot["last_ms"], Config.THREADS, Config.AUTO_REFRESH_SEC,
        )



@app.before_request
def require_basic_auth():
    if not Config.AUTH_REQUIRED:
        return None
    auth = request.authorization
    valid = bool(
        auth
        and Config.AUTH_USER
        and Config.AUTH_PASSWORD
        and hmac.compare_digest(auth.username or "", Config.AUTH_USER)
        and hmac.compare_digest(auth.password or "", Config.AUTH_PASSWORD)
    )
    if valid:
        return None
    return Response(
        "Требуется авторизация",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="RFID KPP"', "Cache-Control": "no-store"},
    )


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================


def db_connect() -> pyodbc.Connection:
    if not Config.DB_CONN_STR:
        raise RuntimeError("Не задан KPP_WEB_DB_CONNECTION / RFID_DB_CONNECTION")
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


def safe_int(value: Any, default: int, minimum: int = 0, maximum: Optional[int] = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    result = max(minimum, result)
    return min(result, maximum) if maximum is not None else result


@app.after_request
def add_security_headers(response: Response) -> Response:
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


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
        WHERE FirstSeen >= DATEADD(hour, -?, GETDATE()) AND IsReel=1
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
        SUM(CASE WHEN TaskMatchType IN ('FULL_TAG_BOTH','FULL_TAG_1C','FULL_TAG_WAREHOUSE') THEN 1 ELSE 0 END) AS MatchFullTag,
        SUM(CASE WHEN TaskMatchType IN ('EPC_UNIQUE_1C','EPC_UNIQUE_WAREHOUSE') THEN 1 ELSE 0 END) AS MatchEpcOnly,
        SUM(CASE WHEN TaskMatchType = 'NOT_REEL' THEN 1 ELSE 0 END) AS MatchNotFound,
        SUM(CASE WHEN TaskMatchType IN ('FULL_TAG_BOTH','FULL_TAG_WAREHOUSE','EPC_UNIQUE_WAREHOUSE') THEN 1 ELSE 0 END) AS MatchWarehouse,
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


def fetch_health() -> Dict[str, Any]:
    query = """
    SELECT
      (SELECT MAX(Id) FROM dbo.RFID_Tags) AS MaxRawId,
      (SELECT TRY_CONVERT(bigint,StateValue) FROM dbo.KPP_RuntimeState WHERE StateKey='LAST_RFID_ID_V3') AS CursorId,
      (SELECT COUNT_BIG(*) FROM dbo.KPP_ActiveRfidSessions) AS ActiveSessions,
      (SELECT COUNT_BIG(*) FROM dbo.KPP_ReelEvents WHERE NeedRecheck=1) AS PendingRecheck,
      (SELECT COUNT_BIG(*) FROM dbo.KPP_ProcessingErrors WHERE ResolvedAt IS NULL) AS OpenErrors,
      (SELECT UpdatedAt FROM dbo.KPP_RuntimeState WHERE StateKey='KPP_V3_HEARTBEAT') AS HeartbeatAt
    """
    with db_connect() as conn:
        cur = conn.cursor(); cur.execute(query); row = cur.fetchone()
    max_raw = int(row[0] or 0)
    cursor = int(row[1] or 0)
    lag = max(0, max_raw - cursor)
    heartbeat = row[5]
    heartbeat_age = (datetime.now() - heartbeat).total_seconds() if heartbeat else None
    status = "OK"
    if int(row[4] or 0) > 0 or (heartbeat_age is not None and heartbeat_age > 120):
        status = "ERROR"
    elif lag > Config.MAX_LIMIT or int(row[3] or 0) > 1000:
        status = "WARNING"
    return {
        "status": status, "max_raw_id": max_raw, "cursor_id": cursor, "raw_lag": lag,
        "active_sessions": int(row[2] or 0), "pending_recheck": int(row[3] or 0),
        "open_processing_errors": int(row[4] or 0),
        "heartbeat_at": heartbeat.isoformat() if heartbeat else None,
        "heartbeat_age_sec": round(heartbeat_age, 1) if heartbeat_age is not None else None,
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
    WHERE FirstSeen >= DATEADD(day, -?, GETDATE()) AND IsReel=1
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
    WHERE FirstSeen >= DATEADD(hour, -?, GETDATE()) AND IsReel=1
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

def _event_filters(
    direction: str = "",
    consensus: str = "",
    match_type: str = "",
    transport: str = "",
    pending: str = "",
    search: str = "",
    objects: str = "reels",
) -> tuple[List[str], List[Any]]:
    where = ["1=1"]
    params: List[Any] = []
    if objects != "all":
        where.append("e.IsReel = 1")
    if direction:
        where.append("e.FinalDirection = ?"); params.append(direction)
    if consensus:
        where.append("e.ConsensusCode = ?"); params.append(consensus)
    if match_type:
        where.append("e.TaskMatchType = ?"); params.append(match_type)
    if transport:
        where.append("e.TransportMode = ?"); params.append(transport)
    if pending == "1":
        where.append("e.NeedRecheck = 1")
    elif pending == "0":
        where.append("e.NeedRecheck = 0")
    if search:
        where.append("(" + " OR ".join([
            "ISNULL(e.EventKey,'') LIKE ?", "ISNULL(e.SourceTag,'') LIKE ?", "ISNULL(e.EPC,'') LIKE ?",
            "ISNULL(e.TID,'') LIKE ?", "ISNULL(e.Task1CDocIds,'') LIKE ?", "ISNULL(e.WarehouseDocIds,'') LIKE ?",
            "ISNULL(CAST(e.Task1CId AS nvarchar(50)),'') LIKE ?", "ISNULL(CAST(e.WarehouseId AS nvarchar(50)),'') LIKE ?",
            "ISNULL(t.SeriesNumber,'') LIKE ?", "ISNULL(e.ReelClassification,'') LIKE ?"
        ]) + ")")
        like = f"%{search}%"
        params.extend([like] * 10)
    return where, params


def fetch_events(
    limit: int,
    offset: int = 0,
    direction: str = "",
    consensus: str = "",
    match_type: str = "",
    transport: str = "",
    pending: str = "",
    search: str = "",
    objects: str = "reels",
) -> List[Dict[str, Any]]:
    limit = max(1, min(limit, Config.MAX_LIMIT))
    offset = max(0, offset)
    where, params = _event_filters(direction, consensus, match_type, transport, pending, search, objects)
    query = f"""
    SELECT
        e.EventId,e.EventKey,e.SourceTag,e.EPC,e.TID,
        e.Task1CId,e.Task1CDt,e.Task1CDocIds,t.SeriesNumber AS Task1CSeriesNumber,e.TaskMatchType,
        e.WarehouseId,e.WarehouseDt,e.WarehouseDocIds,w.SeriesNumber AS WarehouseSeriesNumber,
        e.FirstSeen,e.LastSeen,e.CompletedAt,e.SessionCloseReason,
        e.RfidReadCount,e.DistinctAntennaCount,e.DistinctZoneCount,e.RfidFirstAntenna,e.RfidLastAntenna,
        e.FirstZone,e.LastZone,e.RfidAntennasCsv,e.RfidZonesCsv,e.AvgRSSI,e.MinRSSI,e.MaxRSSI,e.DurationMs,
        e.RfidDirection,e.RfidDirectionScore,
        e.VideoMatched,e.VideoEventId,e.VideoTime,e.VideoDirection,e.VideoTransport,e.VideoTimeDeltaMs,e.VideoScore,
        e.SkudMatched,e.SkudExternalId,e.SkudTime,e.SkudDirection,e.SkudGate,e.SkudPerson,e.SkudCard,e.SkudTimeDeltaMs,e.SkudScore,
        e.FinalDirection,e.ConfidencePct,e.ConsensusCode,e.ScoreIn,e.ScoreOut,e.SourceCount,e.TransportMode,
        e.WarningFlags,e.EvidenceJson,e.NeedRecheck,e.NextRecheckAt,e.RecheckCount,e.LastRecheckAt,
        e.CreatedAt,e.UpdatedAt,e.FinalizedAt,
        e.IsReel,e.ObjectType,e.ReelClassification,e.PassageGroupKey,e.GroupReelCount,
        e.RfidMinRawId,e.RfidMaxRawId,e.SourceTimeQuality,e.ProcessingVersion
    FROM dbo.KPP_ReelEvents e
    LEFT JOIN {Config.TASK_TABLE} t ON t.Id=e.Task1CId
    LEFT JOIN dbo.Warehouse w ON w.Id=e.WarehouseId
    WHERE {' AND '.join(where)}
    ORDER BY e.FirstSeen DESC,e.EventId DESC
    OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
    """
    with db_connect() as conn:
        cur = conn.cursor(); cur.execute(query, [*params, offset, limit])
        return [row_to_dict(cur, row) for row in cur.fetchall()]


def count_events(
    direction: str = "", consensus: str = "", match_type: str = "", transport: str = "",
    pending: str = "", search: str = "", objects: str = "reels",
) -> int:
    where, params = _event_filters(direction, consensus, match_type, transport, pending, search, objects)
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT COUNT_BIG(*) FROM dbo.KPP_ReelEvents e "
            f"LEFT JOIN {Config.TASK_TABLE} t ON t.Id=e.Task1CId "
            f"LEFT JOIN dbo.Warehouse w ON w.Id=e.WarehouseId "
            f"WHERE {' AND '.join(where)}",
            params,
        )
        return int(cur.fetchone()[0])


def fetch_event_details(event_id: int) -> Optional[Dict[str, Any]]:
    query = f"""
    SELECT
        e.*, 
        t.SeriesNumber AS Task1CSeriesNumber,
        w.SeriesNumber AS WarehouseSeriesNumber
    FROM dbo.KPP_ReelEvents e
    LEFT JOIN {Config.TASK_TABLE} t ON t.Id = e.Task1CId
    LEFT JOIN dbo.Warehouse w ON w.Id = e.WarehouseId
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
# ОТЧЕТ RFID / WAREHOUSE
# ============================================================================

RU_MONTHS = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}

REPORT_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="4"><font><name val="Arial"/><sz val="8"/></font><font><name val="Arial"/><sz val="8"/></font><font><name val="Arial"/><b val="false"/><color rgb="FFFFFF"/><sz val="10"/></font><font><name val="Arial"/><b val="true"/><color rgb="003366"/><sz val="8"/></font></fonts>
  <fills count="6"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="4574A0"/><bgColor auto="true"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="C6E2FF"/><bgColor auto="true"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="DCF1FF"/><bgColor auto="true"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="F0FFFF"/><bgColor auto="true"/></patternFill></fill></fills>
  <borders count="6"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"><color rgb="BDC7EB"/></left><right style="thin"><color rgb="BDC7EB"/></right><top style="thin"><color rgb="BDC7EB"/></top><bottom style="thin"><color rgb="BDC7EB"/></bottom><diagonal/></border><border><left style="thin"><color rgb="BDC7EB"/></left><right style="thin"><color rgb="BDC7EB"/></right><top/><bottom/><diagonal/></border><border><left style="thin"><color rgb="BDC7EB"/></left><right style="thin"><color rgb="BDC7EB"/></right><top/><bottom style="thin"><color rgb="BDC7EB"/></bottom><diagonal/></border><border><left style="thin"><color rgb="BDC7EB"/></left><right style="thin"><color rgb="BDC7EB"/></right><top style="thin"><color rgb="BDC7EB"/></top><bottom/><diagonal/></border><border><left style="thin"><color rgb="7D8AB9"/></left><right style="thin"><color rgb="7D8AB9"/></right><top style="thin"><color rgb="7D8AB9"/></top><bottom style="thin"><color rgb="7D8AB9"/></bottom><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="15"><xf numFmtId="0" fontId="0"/><xf numFmtId="0" fontId="0" applyAlignment="true"><alignment horizontal="left"/></xf><xf numFmtId="0" fontId="0" applyAlignment="true"><alignment horizontal="left" vertical="top"/></xf><xf numFmtId="0" fontId="2" fillId="2" borderId="1" applyFont="true" applyFill="true" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="2" borderId="2" applyFont="true" applyFill="true" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="2" borderId="3" applyFont="true" applyFill="true" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="2" borderId="4" applyFont="true" applyFill="true" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="3" fillId="3" borderId="5" applyFont="true" applyFill="true" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="3" fillId="3" borderId="5" applyFont="true" applyFill="true" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top"/></xf><xf numFmtId="0" fontId="3" fillId="4" borderId="5" applyFont="true" applyFill="true" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top" wrapText="1" indent="2"/></xf><xf numFmtId="0" fontId="3" fillId="4" borderId="5" applyFont="true" applyFill="true" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top"/></xf><xf numFmtId="0" fontId="0" fillId="5" borderId="5" applyFill="true" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top" wrapText="1" indent="4"/></xf><xf numFmtId="0" fontId="0" fillId="5" borderId="5" applyFill="true" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="0" borderId="5" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top" wrapText="1" indent="6"/></xf><xf numFmtId="0" fontId="0" borderId="5" applyBorder="true" applyAlignment="true"><alignment horizontal="left" vertical="top" wrapText="1"/></xf></cellXfs>
  <cellStyles count="1"><cellStyle name="Обычный" xfId="0" builtinId="0"/></cellStyles><dxfs count="0"/><tableStyles count="0" defaultTableStyle="TableStyleMedium9" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>"""


def parse_report_date(raw: str, default: date) -> date:
    if not raw:
        return default
    return datetime.strptime(raw[:10], "%Y-%m-%d").date()


def ru_date(dt: Any) -> str:
    if not dt:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", ""))
        except Exception:
            return dt
    return f"{dt.day:02d} {RU_MONTHS.get(dt.month, '')} {dt.year}"


def time_text(dt: Any) -> str:
    if not dt:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", ""))
        except Exception:
            return dt
    return f"{dt.hour}:{dt.minute:02d}:{dt.second:02d}"


def fetch_report_records(date_from: date, date_to: date) -> List[Dict[str, Any]]:
    """Отчёт содержит все подтверждённые катушки, а не только пересечение RFID+1С+Warehouse."""
    query = f"""
    SELECT
        e.EventId,e.EventKey,e.SourceTag,e.EPC,e.TID,e.FirstSeen,e.LastSeen,e.FinalDirection,
        e.Task1CId,e.Task1CDt,e.Task1CDocIds,t.SeriesNumber AS Task1CSeriesNumber,
        e.TaskMatchType,e.RfidReadCount,e.SessionCloseReason,
        e.WarehouseId,e.WarehouseDt,e.WarehouseDocIds,
        COALESCE(w.SeriesNumber,'') AS WarehouseSeriesNumber,
        e.ReelClassification,e.PassageGroupKey,e.GroupReelCount
    FROM dbo.KPP_ReelEvents e
    LEFT JOIN {Config.TASK_TABLE} t ON t.Id=e.Task1CId
    LEFT JOIN dbo.Warehouse w ON w.Id=e.WarehouseId
    WHERE e.FirstSeen>=? AND e.FirstSeen<DATEADD(day,1,?)
      AND e.IsReel=1 AND ISNULL(e.RfidReadCount,0)>0
    ORDER BY COALESCE(e.WarehouseDt,e.FirstSeen),COALESCE(w.SeriesNumber,t.SeriesNumber,''),e.EventId
    """
    with db_connect() as conn:
        cur=conn.cursor(); cur.execute(query, datetime.combine(date_from,datetime.min.time()), datetime.combine(date_to,datetime.min.time()))
        return [row_to_dict(cur,row) for row in cur.fetchall()]


def report_groups(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple, Dict[str, Any]] = {}
    for r in records:
        series = r.get("WarehouseSeriesNumber") or r.get("Task1CSeriesNumber") or "—"
        report_dt = r.get("WarehouseDt") or r.get("FirstSeen")
        dt_key = report_dt.date().isoformat() if isinstance(report_dt, datetime) else str(report_dt or "")
        key = (series, dt_key)
        if key not in grouped:
            grouped[key] = {"series": series, "date": report_dt, "items": []}
        grouped[key]["items"].append(r)
    return list(grouped.values())


def report_preview_html(records: List[Dict[str, Any]], date_from: date, date_to: date) -> str:
    groups = report_groups(records)
    h = []
    h.append('<table class="rfid-report-table">')
    h.append(f'<tr class="preamble"><td>Параметры:</td><td></td><td colspan="4">Период: {date_from.strftime("%d.%m.%Y")} - {date_to.strftime("%d.%m.%Y")}</td></tr>')
    h.append('<tr><td colspan="6" style="border-color:transparent;background:#fff;height:10px"></td></tr>')
    h.append('<tr><td class="hdr" colspan="4">Место</td><td class="hdr">Возвращено в цех</td><td class="hdr">На складе</td></tr>')
    h.append('<tr><td class="hdr" colspan="4">Серия</td><td class="hdr" rowspan="3">Дата события</td><td class="hdr" rowspan="3">Дата события</td></tr>')
    h.append('<tr><td class="hdr" colspan="4">Период</td></tr>')
    h.append('<tr><td class="hdr" colspan="4">Метка</td></tr>')
    h.append(f'<tr><td class="place" colspan="4">{html_escape(Config.REPORT_PLACE)}</td><td class="place"></td><td class="place"></td></tr>')
    if not groups:
        h.append('<tr><td colspan="6" class="date-row">Нет подтвержденных событий за выбранный период</td></tr>')
    for g in groups:
        h.append(f'<tr><td class="series" colspan="4">{html_escape(str(g["series"]))}</td><td class="series"></td><td class="series"></td></tr>')
        first = True
        for r in g["items"]:
            event_time = time_text(r.get("FirstSeen")) if r.get("FinalDirection") == "IN" else ""
            wh_time = time_text(r.get("WarehouseDt"))
            if first:
                h.append(f'<tr><td class="date-row" colspan="4">{html_escape(ru_date(g["date"]))}</td><td class="date-row">{html_escape(event_time)}</td><td class="date-row">{html_escape(wh_time)}</td></tr>')
                first = False
            h.append(f'<tr><td class="tag-row" colspan="4">{html_escape(str(r.get("SourceTag") or ""))}</td><td>{html_escape(event_time)}</td><td>{html_escape(wh_time)}</td></tr>')
    h.append('</table>')
    return "".join(h)


def xlsx_col(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def xlsx_cell(row: int, col: int, value: Any, style: int = 0) -> str:
    ref = f"{xlsx_col(col)}{row}"
    s = f' s="{style}"' if style is not None else ""
    if value is None or value == "":
        return f'<c r="{ref}"{s}/>'
    return f'<c r="{ref}"{s} t="inlineStr"><is><t>{xml_escape(str(value))}</t></is></c>'


def generate_report_xlsx(records: List[Dict[str, Any]], date_from: date, date_to: date) -> bytes:
    groups = report_groups(records)
    rows: List[Dict[str, Any]] = []
    rows.append({"h": 10, "cells": []})
    rows.append({"h": 11, "cells": [(1, "Параметры:", 2), (3, f"Период: {date_from.strftime('%d.%m.%Y')} - {date_to.strftime('%d.%m.%Y')}", 2)]})
    rows.append({"h": 10, "cells": []})
    rows.append({"h": 13, "cells": [(1, "Место", 3), (2, "", 3), (3, "", 3), (4, "", 3), (5, "Возвращено в цех", 3), (6, "На складе", 3)]})
    rows.append({"h": 13, "cells": [(1, "Серия", 3), (2, "", 3), (3, "", 3), (4, "", 3), (5, "Дата события", 6), (6, "Дата события", 6)]})
    rows.append({"h": 13, "cells": [(1, "Период", 3), (2, "", 3), (3, "", 3), (4, "", 3), (5, "", 4), (6, "", 4)]})
    rows.append({"h": 13, "cells": [(1, "Метка", 3), (2, "", 3), (3, "", 3), (4, "", 3), (5, "", 5), (6, "", 5)]})
    rows.append({"h": 11, "cells": [(1, Config.REPORT_PLACE, 7), (2, "", 7), (3, "", 7), (4, "", 7), (5, "", 8), (6, "", 8)]})
    if not groups:
        rows.append({"h": 11, "cells": [(1, "Нет подтвержденных событий за выбранный период", 11), (2, "", 11), (3, "", 11), (4, "", 11), (5, "", 12), (6, "", 12)]})
    for g in groups:
        rows.append({"h": 11, "cells": [(1, g["series"], 9), (2, "", 9), (3, "", 9), (4, "", 9), (5, "", 10), (6, "", 10)]})
        first = True
        for r in g["items"]:
            event_time = time_text(r.get("FirstSeen")) if r.get("FinalDirection") == "IN" else ""
            wh_time = time_text(r.get("WarehouseDt"))
            if first:
                rows.append({"h": 11, "cells": [(1, ru_date(g["date"]), 11), (2, "", 11), (3, "", 11), (4, "", 11), (5, event_time, 12), (6, wh_time, 12)]})
                first = False
            rows.append({"h": 11, "cells": [(1, r.get("SourceTag") or "", 13), (2, "", 13), (3, "", 13), (4, "", 13), (5, event_time, 14), (6, wh_time, 14)]})
    last_row = len(rows)
    sheet_rows = []
    merges = ["A4:D4", "A5:D5", "E5:E7", "F5:F7", "A6:D6", "A7:D7", "A8:D8"]
    for idx, row in enumerate(rows, start=1):
        sheet_rows.append(f'<row r="{idx}" ht="{row["h"]}" customHeight="true">')
        for col, value, style in row["cells"]:
            sheet_rows.append(xlsx_cell(idx, col, value, style))
        sheet_rows.append("</row>")
        if idx >= 9:
            merges.append(f"A{idx}:D{idx}")
    merge_xml = f'<mergeCells count="{len(merges)}">' + "".join(f'<mergeCell ref="{m}"/>' for m in merges) + "</mergeCells>"
    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetPr><outlinePr summaryBelow="false" summaryRight="false"/><pageSetUpPr autoPageBreaks="false"/></sheetPr>
  <dimension ref="A1:F{last_row}"/>
  <sheetViews><sheetView tabSelected="true" workbookViewId="0"/></sheetViews>
  <sheetFormatPr defaultColWidth="10.5" customHeight="true" defaultRowHeight="11.429"/>
  <cols>
    <col min="1" max="1" width="10.5" style="1" customWidth="true"/>
    <col min="2" max="2" width="2.66796875" style="1" customWidth="true"/>
    <col min="3" max="3" width="29.66796875" style="1" customWidth="true"/>
    <col min="4" max="4" width="22.66796875" style="1" customWidth="true"/>
    <col min="5" max="5" width="23" style="1" customWidth="true"/>
    <col min="6" max="6" width="14" style="1" customWidth="true"/>
  </cols>
  <sheetData>{''.join(sheet_rows)}</sheetData>
  {merge_xml}
  <pageMargins left="0.75" right="0.75" top="1" bottom="1" header="0.5" footer="0.5"/>
</worksheet>"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="TDSheet" sheetId="1" r:id="rId1"/></sheets></workbook>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>"""
    wb_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>"""
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", REPORT_STYLES_XML)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return bio.getvalue()


# ============================================================================
# AI-ПОМОЩНИК
# ============================================================================

def fetch_assistant_context(question: str, event_id: Optional[int]) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {"summary": fetch_summary(Config.SUMMARY_HOURS), "runtime": fetch_runtime_state()}
    if event_id:
        ctx["event"] = fetch_event_details(int(event_id))
    tokens = [x for x in re.findall(r"[0-9]{3,6}/[0-9]{2,4}|[A-Fa-f0-9]{16,}|[0-9]{4,}", question or "")[:3]]
    if tokens:
        found: List[Dict[str, Any]] = []
        for token in tokens:
            found.extend(fetch_events(limit=5, search=token))
        ctx["found_events"] = found[:10]
    return ctx


def call_lm_studio(question: str, context: Dict[str, Any]) -> str:
    system = (
        "Ты помощник web-панели RFID КПП. Отвечай коротко и по делу. "
        "Можно использовать простой Markdown: списки, жирный текст, таблицы и короткие блоки кода. "
        "Помогай искать карточки, объяснять статусы, предупреждения, направление, связь с 1С и Warehouse. "
        "Не выдумывай данные: если факта нет в контексте, скажи что надо открыть карточку или уточнить период."
    )
    user = {"question": question, "context": context}
    payload = {
        "model": Config.AI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False, cls=JsonEncoder)[:24000]},
        ],
        "temperature": 0.2,
        "max_tokens": 800,
    }
    errors = []
    for base in [Config.AI_BASE_URL, Config.AI_FALLBACK_BASE_URL]:
        if not base:
            continue
        try:
            req = urllib.request.Request(
                f"{base}/v1/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=Config.AI_TIMEOUT_SEC) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip() or "Модель вернула пустой ответ."
        except Exception as exc:
            errors.append(f"{base}: {exc}")
    return "Помощник сейчас недоступен. Ошибка подключения к LM Studio: " + " | ".join(errors)


# ============================================================================
# WEB
# ============================================================================


PAGE = r"""
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
      background: #ffffff;
      color: #1f4e79;
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
      color: #1f4e79;
    }
    .subtitle {
      color: #1f4e79;
      font-size: 14px;
      line-height: 1.5;
      opacity: 0.8;
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

    .report-actions {
      display: flex;
      gap: 10px;
      align-items: center;
      justify-content: flex-end;
      margin-bottom: 14px;
    }
    .report-modal-card { width: min(1180px, 100%); }
    .report-controls {
      display: grid;
      grid-template-columns: 1fr 1fr auto auto;
      gap: 12px;
      align-items: end;
      margin-bottom: 16px;
    }
    .report-preview-wrap {
      background: #f2f6fb;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px;
      overflow: auto;
      max-height: 66vh;
    }
    .rfid-report-table {
      border-collapse: collapse;
      font-family: Arial, sans-serif;
      font-size: 12px;
      min-width: 720px;
      background: #fff;
    }
    .rfid-report-table td {
      border: 1px solid #7D8AB9;
      padding: 5px 7px;
      height: 18px;
      vertical-align: top;
      text-align: left;
    }
    .rfid-report-table .preamble td {
      border-color: transparent;
      background: #fff;
      color: #003366;
      font-weight: 700;
    }
    .rfid-report-table .hdr {
      background: #4574A0;
      color: #fff;
      font-weight: 700;
    }
    .rfid-report-table .place {
      background: #C6E2FF;
      color: #003366;
      font-weight: 700;
    }
    .rfid-report-table .series {
      background: #DCF1FF;
      color: #003366;
      font-weight: 700;
      padding-left: 18px;
    }
    .rfid-report-table .date-row {
      background: #F0FFFF;
      color: #111827;
      padding-left: 28px;
    }
    .rfid-report-table .tag-row {
      background: #fff;
      color: #111827;
      font-family: Consolas, monospace;
      padding-left: 38px;
      font-size: 11px;
    }
    .assistant-fab {
      position: fixed;
      right: 22px;
      bottom: 22px;
      z-index: 50;
      width: 58px;
      height: 58px;
      border-radius: 50%;
      background: #1f4e79;
      color: #fff;
      border: 2px solid #dbe8f5;
      box-shadow: 0 10px 28px rgba(15, 23, 42, 0.28);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 28px;
      cursor: pointer;
    }
    .assistant-panel {
      position: fixed;
      right: 22px;
      bottom: 92px;
      z-index: 50;
      width: min(420px, calc(100vw - 44px));
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 10px;
      box-shadow: 0 22px 60px rgba(15, 23, 42, 0.25);
      display: none;
      overflow: hidden;
    }
    .assistant-panel.open { display: block; }
    .assistant-head {
      background: #1f4e79;
      color: #fff;
      padding: 12px 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-weight: 900;
    }
    .assistant-body {
      padding: 14px;
      display: grid;
      gap: 10px;
    }
    .assistant-answer {
      min-height: 90px;
      max-height: 260px;
      overflow: auto;
      border: 1px solid var(--border);
      background: #f7fafc;
      border-radius: 8px;
      padding: 10px;
      font-size: 13px;
      line-height: 1.45;
    }
    .assistant-answer.markdown { white-space: normal; }
    .assistant-answer.markdown p { margin: 0 0 10px 0; }
    .assistant-answer.markdown p:last-child { margin-bottom: 0; }
    .assistant-answer.markdown ul,
    .assistant-answer.markdown ol { margin: 6px 0 10px 22px; padding: 0; }
    .assistant-answer.markdown li { margin: 3px 0; }
    .assistant-answer.markdown h1,
    .assistant-answer.markdown h2,
    .assistant-answer.markdown h3,
    .assistant-answer.markdown h4 { margin: 10px 0 6px; color: #0f172a; line-height: 1.25; }
    .assistant-answer.markdown h1 { font-size: 18px; }
    .assistant-answer.markdown h2 { font-size: 16px; }
    .assistant-answer.markdown h3 { font-size: 15px; }
    .assistant-answer.markdown h4 { font-size: 14px; }
    .assistant-answer.markdown code {
      font-family: Consolas, monospace;
      background: #e5eaf0;
      border: 1px solid #d2dae4;
      border-radius: 4px;
      padding: 1px 4px;
      font-size: 12px;
    }
    .assistant-answer.markdown pre {
      background: #0b1220;
      color: #e5e7eb;
      border-radius: 8px;
      padding: 10px;
      overflow: auto;
      white-space: pre;
      margin: 8px 0 10px;
    }
    .assistant-answer.markdown pre code {
      background: transparent;
      border: 0;
      color: inherit;
      padding: 0;
    }
    .assistant-answer.markdown blockquote {
      border-left: 3px solid #1f4e79;
      margin: 8px 0;
      padding: 6px 10px;
      background: #eef5fb;
      color: #334155;
    }
    .assistant-answer.markdown table {
      width: 100%;
      border-collapse: collapse;
      margin: 8px 0 10px;
      font-size: 12px;
    }
    .assistant-answer.markdown th,
    .assistant-answer.markdown td {
      border: 1px solid #d8e0ea;
      padding: 5px 7px;
      vertical-align: top;
    }
    .assistant-answer.markdown th { background: #e8f1f8; font-weight: 800; }
    .assistant-input-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
    }
    .card-question {
      margin-top: 14px;
      border-top: 1px solid var(--border);
      padding-top: 14px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
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
        <h1 class="title">КПП</h1>
        <div class="subtitle">Промышленный мониторинг RFID-объектов с отдельным учётом катушек</div>
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

    <div class="report-actions">
      <button class="btn-primary" onclick="openReportModal()">Скачать отчет</button>
    </div>

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
            <div class="logic-item"><strong>3. 1С и Warehouse</strong> Катушкой считается только RFID-объект, подтверждённый полной меткой в 1С/RfidTags или Warehouse в окне ±24 часа. Остальные RFID сохраняются отдельно и не входят в счётчик катушек.</div>
            <div class="logic-item"><strong>4. Склад без КПП</strong> Warehouse используется как подтверждение типа объекта и отдельный источник данных. Запись склада без RFID-проезда не увеличивает счётчик проходов.</div>
          </div>
        </div>
      </div>
    </div>

    <div class="card filters">
      <div class="field">
        <label>Объекты</label>
        <select id="fObjects">
          <option value="reels" selected>Только катушки</option>
          <option value="all">Все RFID-объекты</option>
        </select>
      </div>
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
          <option value="FULL_TAG_BOTH">ПОЛНАЯ МЕТКА: 1С + СКЛАД</option>
          <option value="FULL_TAG_1C">ПОЛНАЯ МЕТКА: 1С</option>
          <option value="FULL_TAG_WAREHOUSE">ПОЛНАЯ МЕТКА: СКЛАД</option>
          <option value="EPC_UNIQUE_1C">УНИКАЛЬНЫЙ EPC: 1С</option>
          <option value="EPC_UNIQUE_WAREHOUSE">УНИКАЛЬНЫЙ EPC: СКЛАД</option>
          <option value="NOT_REEL">НЕ КАТУШКА / НЕ ПОДТВЕРЖДЕНО</option>        </select>
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
          <div class="panel-desc">По умолчанию показаны только подтверждённые катушки. Неизвестные RFID-объекты доступны отдельным фильтром.</div>
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
      <div style="display:flex;gap:8px;justify-content:flex-end;padding:12px 16px;align-items:center;">
        <button class="btn-light" id="prevPage" onclick="changePage(-1)">Назад</button>
        <span class="hint" id="pageMeta">—</span>
        <button class="btn-light" id="nextPage" onclick="changePage(1)">Далее</button>
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

  <div class="modal" id="reportModal">
    <div class="modal-card report-modal-card">
      <div class="modal-head">
        <div>
          <h3 class="modal-title">Отчет RFID</h3>
          <div class="modal-sub">Все RFID-проходы, классифицированные как катушки по полной метке 1С или Warehouse в окне ±24 часа.</div>
        </div>
        <button class="btn-light" onclick="closeReportModal()">Закрыть</button>
      </div>
      <div class="modal-body">
        <div class="report-controls">
          <div class="field">
            <label>Дата от</label>
            <input id="reportDateFrom" type="date" />
          </div>
          <div class="field">
            <label>Дата до</label>
            <input id="reportDateTo" type="date" />
          </div>
          <button class="btn-light" onclick="loadReportPreview()">Предпросмотр</button>
          <button class="btn-primary" onclick="downloadReport()">Скачать XLSX</button>
        </div>
        <div id="reportMeta" class="panel-desc">—</div>
        <div class="report-preview-wrap" id="reportPreview">Выберите период и нажмите «Предпросмотр».</div>
      </div>
    </div>
  </div>

  <div class="assistant-fab" onclick="toggleAssistant()" title="Помощник">📎</div>
  <div class="assistant-panel" id="assistantPanel">
    <div class="assistant-head">
      <span>Помощник КПП</span>
      <button class="btn-light" onclick="toggleAssistant(false)">×</button>
    </div>
    <div class="assistant-body">
      <div class="panel-desc">Можно спросить: «найди 4223/26», «почему событие не найдено в 1С», «сколько выездов за сутки».</div>
      <div class="assistant-answer markdown" id="assistantAnswer"><p>Готов помочь по навигации, поиску карточек, объяснению статусов и статистике.</p></div>
      <div class="assistant-input-row">
        <input id="assistantQuestion" placeholder="Введите вопрос..." />
        <button class="btn-primary" onclick="askAssistant()">Спросить</button>
      </div>
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
        FULL_TAG_BOTH: 'ПОЛНАЯ МЕТКА: 1С + СКЛАД',
        FULL_TAG_1C: 'ПОЛНАЯ МЕТКА: 1С',
        FULL_TAG_WAREHOUSE: 'ПОЛНАЯ МЕТКА: СКЛАД',
        EPC_UNIQUE_1C: 'УНИКАЛЬНЫЙ EPC: 1С',
        EPC_UNIQUE_WAREHOUSE: 'УНИКАЛЬНЫЙ EPC: СКЛАД',
        NOT_REEL: 'НЕ КАТУШКА / НЕ ПОДТВЕРЖДЕНО'
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
      if (['FULL_TAG_BOTH','FULL_TAG_WAREHOUSE','EPC_UNIQUE_WAREHOUSE'].includes(v)) return 'tag tag-warehouse';
      return v === 'NOT_REEL' ? 'tag tag-not-found' : 'tag tag-match';
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;');
    }

    function inlineMarkdownSafe(value) {
      let s = escapeHtml(value);
      s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
      s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      s = s.replace(/__([^_]+)__/g, '<strong>$1</strong>');
      s = s.replace(/(^|\s)\*([^*\n]+)\*(?=\s|$|[.,;:!?])/g, '$1<em>$2</em>');
      s = s.replace(/(^|\s)_([^_\n]+)_(?=\s|$|[.,;:!?])/g, '$1<em>$2</em>');
      return s;
    }

    function isMarkdownTableSeparator(line) {
      return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line || '');
    }

    function splitMarkdownRow(line) {
      let s = String(line || '').trim();
      if (s.startsWith('|')) s = s.slice(1);
      if (s.endsWith('|')) s = s.slice(0, -1);
      return s.split('|').map(x => x.trim());
    }

    function renderMarkdownTable(headerLine, bodyLines) {
      const headers = splitMarkdownRow(headerLine);
      const head = `<thead><tr>${headers.map(h => `<th>${inlineMarkdownSafe(h)}</th>`).join('')}</tr></thead>`;
      const rows = bodyLines.map(line => {
        const cells = splitMarkdownRow(line);
        return `<tr>${cells.map(c => `<td>${inlineMarkdownSafe(c)}</td>`).join('')}</tr>`;
      }).join('');
      return `<table>${head}<tbody>${rows}</tbody></table>`;
    }

    function renderAssistantMarkdown(raw) {
      const lines = String(raw ?? '').replace(/\r\n/g, '\n').split('\n');
      let html = '';
      let paragraph = [];
      let listType = null;
      let inCode = false;
      let codeLines = [];

      function flushParagraph() {
        if (!paragraph.length) return;
        html += `<p>${paragraph.map(inlineMarkdownSafe).join('<br>')}</p>`;
        paragraph = [];
      }
      function closeList() {
        if (!listType) return;
        html += `</${listType}>`;
        listType = null;
      }
      function openList(type) {
        if (listType === type) return;
        closeList();
        flushParagraph();
        listType = type;
        html += `<${type}>`;
      }

      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const trimmed = line.trim();

        if (trimmed.startsWith('```')) {
          if (inCode) {
            html += `<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`;
            codeLines = [];
            inCode = false;
          } else {
            flushParagraph();
            closeList();
            inCode = true;
            codeLines = [];
          }
          continue;
        }
        if (inCode) {
          codeLines.push(line);
          continue;
        }

        if (trimmed === '') {
          flushParagraph();
          closeList();
          continue;
        }

        if (i + 1 < lines.length && line.includes('|') && isMarkdownTableSeparator(lines[i + 1])) {
          flushParagraph();
          closeList();
          const body = [];
          i += 2;
          while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
            body.push(lines[i]);
            i += 1;
          }
          i -= 1;
          html += renderMarkdownTable(line, body);
          continue;
        }

        const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
        if (heading) {
          flushParagraph();
          closeList();
          const level = heading[1].length;
          html += `<h${level}>${inlineMarkdownSafe(heading[2])}</h${level}>`;
          continue;
        }

        const quote = trimmed.match(/^>\s?(.+)$/);
        if (quote) {
          flushParagraph();
          closeList();
          html += `<blockquote>${inlineMarkdownSafe(quote[1])}</blockquote>`;
          continue;
        }

        const ul = trimmed.match(/^[-*]\s+(.+)$/);
        if (ul) {
          openList('ul');
          html += `<li>${inlineMarkdownSafe(ul[1])}</li>`;
          continue;
        }

        const ol = trimmed.match(/^\d+[.)]\s+(.+)$/);
        if (ol) {
          openList('ol');
          html += `<li>${inlineMarkdownSafe(ol[1])}</li>`;
          continue;
        }

        closeList();
        paragraph.push(line);
      }
      if (inCode) html += `<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`;
      flushParagraph();
      closeList();
      return html || '<p>Ответ пустой.</p>';
    }

    async function getJson(url) {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    }


    function isoDate(d) {
      const z = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
      return z.toISOString().slice(0, 10);
    }

    function ensureReportDates() {
      const to = document.getElementById('reportDateTo');
      const from = document.getElementById('reportDateFrom');
      if (!to.value) to.value = isoDate(new Date());
      if (!from.value) {
        const d = new Date();
        d.setDate(d.getDate() - 7);
        from.value = isoDate(d);
      }
    }

    function openReportModal() {
      ensureReportDates();
      document.getElementById('reportModal').classList.add('open');
      loadReportPreview();
    }

    function closeReportModal() {
      document.getElementById('reportModal').classList.remove('open');
    }

    async function loadReportPreview() {
      ensureReportDates();
      const from = document.getElementById('reportDateFrom').value;
      const to = document.getElementById('reportDateTo').value;
      const data = await getJson(`/api/report/preview?date_from=${encodeURIComponent(from)}&date_to=${encodeURIComponent(to)}`);
      document.getElementById('reportMeta').textContent = `Период: ${data.date_from} - ${data.date_to} • строк: ${data.total_rows} • серий: ${data.total_series}`;
      document.getElementById('reportPreview').innerHTML = data.html;
    }

    function downloadReport() {
      ensureReportDates();
      const from = document.getElementById('reportDateFrom').value;
      const to = document.getElementById('reportDateTo').value;
      window.location.href = `/api/report/download?date_from=${encodeURIComponent(from)}&date_to=${encodeURIComponent(to)}`;
    }

    let assistantEventId = null;
    let currentOffset = 0;
    let currentTotal = 0;

    function toggleAssistant(force) {
      const p = document.getElementById('assistantPanel');
      const open = typeof force === 'boolean' ? force : !p.classList.contains('open');
      p.classList.toggle('open', open);
      if (open) document.getElementById('assistantQuestion').focus();
    }

    async function askAssistant(questionOverride=null, eventIdOverride=null) {
      const qEl = document.getElementById('assistantQuestion');
      const question = (questionOverride || qEl.value || '').trim();
      if (!question) return;
      const ans = document.getElementById('assistantAnswer');
      ans.classList.add('markdown');
      ans.textContent = 'Думаю...';
      toggleAssistant(true);
      try {
        const res = await fetch('/api/assistant', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({question, event_id: eventIdOverride ?? assistantEventId})
        });
        const data = await res.json();
        ans.innerHTML = renderAssistantMarkdown(data.answer || data.error || 'Ответ пустой.');
      } catch (e) {
        ans.innerHTML = renderAssistantMarkdown(`Ошибка помощника: ${e}`);
      }
    }

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && document.activeElement && document.activeElement.id === 'assistantQuestion') askAssistant();
    });

    function clearFilters() {
      document.getElementById('fObjects').value = 'reels';
      document.getElementById('fDirection').value = '';
      document.getElementById('fConsensus').value = '';
      document.getElementById('fMatchType').value = '';
      document.getElementById('fTransport').value = '';
      document.getElementById('fPending').value = '';
      document.getElementById('fLimit').value = '200';
      document.getElementById('fSearch').value = '';
      currentOffset = 0;
      loadAll();
    }

    function changePage(delta) {
      const limit = Number(document.getElementById('fLimit').value || 200);
      currentOffset = Math.max(0, currentOffset + delta * limit);
      if (currentOffset >= currentTotal) currentOffset = Math.max(0, currentTotal - limit);
      loadAll(false);
    }

    function buildEventsUrl() {
      const params = new URLSearchParams();
      const mapping = {
        objects: document.getElementById('fObjects').value,
        offset: String(currentOffset),
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
      assistantEventId = eventId;
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

        <div class="card-question">
          <input id="cardQuestionInput" placeholder="Спросить помощника по этой карточке..." />
          <button class="btn-primary" onclick="askAssistant(document.getElementById('cardQuestionInput').value, ${data.EventId})">Спросить</button>
        </div>

        <div class="expert-section">
          <details class="expert-block">
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

    async function loadAll(resetPage=true) {
      if (resetPage) currentOffset = 0;
      const [runtime, summary, charts, warnings, events] = await Promise.all([
        getJson('/api/runtime'),
        getJson('/api/summary'),
        getJson('/api/charts'),
        getJson('/api/warnings'),
        getJson(buildEventsUrl()),
      ]);

      const lastRfid = runtime.LAST_RFID_ID_V3 || runtime.LAST_RFID_ID || {};
      document.getElementById('lastRfidId').textContent = lastRfid.value ?? '—';
      document.getElementById('lastRfidUpdated').textContent = lastRfid.updated_at ? `Обновлено: ${fmtDate(lastRfid.updated_at)}` : 'Состояние не найдено';
      document.getElementById('serverTime').textContent = `Сервер: ${fmtDate(summary.server_time)}`;
      document.getElementById('footerInfo').textContent = `Окно сводки: последние {{ summary_hours }} ч • График: {{ chart_days }} д • Последнее обновление: ${fmtDate(summary.server_time)}`;

      renderMetrics(summary);
      renderWarnings(warnings.items);
      renderTimeline(charts.series);
      renderMatchChart(summary);
      currentTotal = Number(events.total || 0);
      renderEvents(events.items);
      const limit = Number(document.getElementById('fLimit').value || 200);
      const from = currentTotal ? currentOffset + 1 : 0;
      const to = Math.min(currentOffset + events.items.length, currentTotal);
      document.getElementById('eventsMeta').textContent = `Показано ${from}–${to} из ${currentTotal}`;
      document.getElementById('pageMeta').textContent = `${from}–${to} / ${currentTotal}`;
      document.getElementById('prevPage').disabled = currentOffset <= 0;
      document.getElementById('nextPage').disabled = currentOffset + limit >= currentTotal;
    }

    document.getElementById('fSearch').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') loadAll();
    });

    document.getElementById('modal').addEventListener('click', (e) => {
      if (e.target.id === 'modal') closeModal();
    });
    document.getElementById('reportModal').addEventListener('click', (e) => {
      if (e.target.id === 'reportModal') closeReportModal();
    });

    loadAll();
    setInterval(() => loadAll(false), REFRESH_SEC * 1000);
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
    return cached_json("runtime", 5.0, fetch_runtime_state)


@app.route("/api/health")
def api_health() -> Response:
    data = fetch_health()
    status_code = 200 if data["status"] != "ERROR" else 503
    return Response(json.dumps(data, cls=JsonEncoder), status=status_code, mimetype="application/json")


@app.route("/api/summary")
def api_summary() -> Response:
    def build():
        data = fetch_summary(Config.SUMMARY_HOURS)
        data["server_time"] = datetime.now().isoformat()
        return data
    return cached_json(f"summary:{Config.SUMMARY_HOURS}", 10.0, build)


@app.route("/api/charts")
def api_charts() -> Response:
    return cached_json(f"charts:{Config.CHART_DAYS}", 30.0, lambda: fetch_chart_series(Config.CHART_DAYS))


@app.route("/api/warnings")
def api_warnings() -> Response:
    return cached_json(
        f"warnings:{Config.SUMMARY_HOURS}", 15.0,
        lambda: {"items": fetch_top_warnings(Config.SUMMARY_HOURS)},
    )


@app.route("/api/events")
def api_events() -> Response:
    limit = safe_int(request.args.get("limit"), Config.DEFAULT_LIMIT, 1, Config.MAX_LIMIT)
    offset = safe_int(request.args.get("offset"), 0, 0)
    filters = dict(
        direction=request.args.get("direction", "").strip(),
        consensus=request.args.get("consensus", "").strip(),
        match_type=request.args.get("match_type", "").strip(),
        transport=request.args.get("transport", "").strip(),
        pending=request.args.get("pending", "").strip(),
        search=request.args.get("search", "").strip(),
        objects=request.args.get("objects", "reels").strip(),
    )
    items = fetch_events(limit=limit, offset=offset, **filters)
    total = count_events(**filters)
    return Response(json.dumps({"items": items, "total": total, "offset": offset, "limit": limit}, cls=JsonEncoder), mimetype="application/json")


@app.route("/api/event/<int:event_id>")
def api_event(event_id: int) -> Response:
    item = fetch_event_details(event_id)
    if not item:
        return Response(json.dumps({"ошибка": "не найдено"}), status=404, mimetype="application/json")
    return Response(json.dumps(item, cls=JsonEncoder), mimetype="application/json")


@app.route("/api/image/<int:video_event_id>")
def api_image(video_event_id: int) -> Response:
    query = "SELECT TOP 1 ImageData,ImageBase64,ImageFormat FROM dbo.ReelTransitions WHERE Id=?"
    with db_connect() as conn:
        cur=conn.cursor(); cur.execute(query,video_event_id); row=cur.fetchone()
        if not row:
            return Response(b"",status=404)
        image_data, legacy, image_format = row
        data = bytes(image_data) if image_data else None
        if not data and legacy:
            if isinstance(legacy,(bytes,bytearray,memoryview)):
                candidate=bytes(legacy)
                data=candidate if candidate.startswith(b"\xff\xd8") else None
            elif isinstance(legacy,str):
                try:
                    candidate=base64.b64decode(legacy,validate=True)
                    data=candidate if candidate.startswith(b"\xff\xd8") else None
                except Exception:
                    data=None
        if not data:
            return Response(b"",status=404)
        fmt=str(image_format or "jpg").lower()
        return Response(data,mimetype="image/jpeg" if fmt in {"jpg","jpeg"} else f"image/{fmt}")



@app.route("/api/report/preview")
def api_report_preview() -> Response:
    today = datetime.now().date()
    date_to = parse_report_date(request.args.get("date_to", ""), today)
    date_from = parse_report_date(request.args.get("date_from", ""), date_to - timedelta(days=7))
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    records = fetch_report_records(date_from, date_to)
    groups = report_groups(records)
    data = {"date_from": date_from.strftime("%d.%m.%Y"), "date_to": date_to.strftime("%d.%m.%Y"), "total_rows": len(records), "total_series": len(groups), "html": report_preview_html(records, date_from, date_to)}
    return Response(json.dumps(data, cls=JsonEncoder), mimetype="application/json")


@app.route("/api/report/download")
def api_report_download() -> Response:
    today = datetime.now().date()
    date_to = parse_report_date(request.args.get("date_to", ""), today)
    date_from = parse_report_date(request.args.get("date_from", ""), date_to - timedelta(days=7))
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    records = fetch_report_records(date_from, date_to)
    xlsx = generate_report_xlsx(records, date_from, date_to)
    filename = f"RFID_KPP_{date_from.strftime('%Y%m%d')}_{date_to.strftime('%Y%m%d')}.xlsx"
    return Response(xlsx, headers={"Content-Disposition": f"attachment; filename={filename}", "Cache-Control": "no-store"}, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/api/assistant", methods=["POST"])
def api_assistant() -> Response:
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question") or "").strip()
    event_id = payload.get("event_id")
    if not question:
        return Response(json.dumps({"error": "Пустой вопрос"}, ensure_ascii=False), status=400, mimetype="application/json")
    try:
        ctx = fetch_assistant_context(question, int(event_id) if event_id else None)
        answer = call_lm_studio(question, ctx)
        return Response(json.dumps({"answer": answer}, ensure_ascii=False, cls=JsonEncoder), mimetype="application/json")
    except Exception as exc:
        return Response(json.dumps({"error": str(exc)}, ensure_ascii=False), status=500, mimetype="application/json")


@app.route("/favicon.ico")
def favicon() -> Response:
    return Response(status=204)


if __name__ == "__main__":
    instance_lock = SingleInstanceLock(Config.LOCK_PATH)
    if not Config.DB_CONN_STR:
        raise RuntimeError("Не задан KPP_WEB_DB_CONNECTION / RFID_DB_CONNECTION")
    if Config.AUTH_REQUIRED and (not Config.AUTH_USER or not Config.AUTH_PASSWORD):
        raise RuntimeError("При KPP_WEB_AUTH_REQUIRED=1 задайте KPP_WEB_AUTH_USER и KPP_WEB_AUTH_PASSWORD")
    print("\n" + "█" * 100)
    print("🌐 КПП • WEB v3.4 катушки отделены от прочих RFID-объектов")
    print("█" * 100)
    print(f"Адрес: {Config.HOST}")
    print(f"Порт: {Config.PORT}")
    print(f"Окно сводки: {Config.SUMMARY_HOURS} ч")
    print(f"Окно графика: {Config.CHART_DAYS} д")
    print(f"Обновление: {Config.AUTO_REFRESH_SEC} с")
    print(f"Потоков WEB: {Config.THREADS}; heartbeat: {Config.HEARTBEAT_SEC:.0f} с")
    print("█" * 100)
    threading.Thread(target=web_heartbeat, name="web-heartbeat", daemon=True).start()
    if Config.DEBUG:
        app.run(host=Config.HOST, port=Config.PORT, debug=True, use_reloader=False)
    else:
        from waitress import serve
        serve(app, host=Config.HOST, port=Config.PORT, threads=Config.THREADS, channel_timeout=30, cleanup_interval=10)
