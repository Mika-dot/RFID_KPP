#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Надежный монитор КПП для катушек с RFID.

Версия с ПОЛНОЙ ПЕРЕСБОРКОЙ KPP_ReelEvents:
- умеет заново пройти ВСЮ историю RFID из dbo.RFID_Tags;
- заново загружает задачи 1С не только инкрементально, а полностью;
- пересобирает и заново сопоставляет события для таблицы dbo.KPP_ReelEvents;
- после полной пересборки может перейти в обычный live-режим;
- state'ы LAST_RFID_ID / LAST_1C_TASK_ROW_ID при полном rebuild не мешают пересборке.

Ожидаемый сценарий:
1) очищаете dbo.KPP_ReelEvents;
2) запускаете этот скрипт с FULL_REBUILD_ON_START=1;
3) скрипт пересобирает все исторические события;
4) затем продолжает обычный мониторинг хвоста.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple

import pyodbc


class Config:
    # --- Подключения ---
    KPP_CONN_STR = os.getenv(
        "KPP_CONN_STR",
        "DRIVER={ODBC Driver 18 for SQL Server};"
        "SERVER=SRV-SQL4.MKM.LAN;"
        "DATABASE=1CTgSend;"
        "UID=TgSendUser;"
        "PWD=Shu_uc3i;"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;",
    )
    TASK_CONN_STR = os.getenv("KPP_TASK_CONN_STR", KPP_CONN_STR)

    # --- Таблицы ---
    RFID_TABLE = os.getenv("KPP_RFID_TABLE", "dbo.RFID_Tags")
    VIDEO_TABLE = os.getenv("KPP_VIDEO_TABLE", "dbo.ReelTransitions")
    SKUD_TABLE = os.getenv("KPP_SKUD_TABLE", "dbo.RusGuardLogs")
    TASK_TABLE = os.getenv("KPP_TASK_TABLE", "dbo.RfidTags")
    EVENT_TABLE = os.getenv("KPP_EVENT_TABLE", "dbo.KPP_ReelEvents")
    STATE_TABLE = os.getenv("KPP_STATE_TABLE", "dbo.KPP_RuntimeState")

    # --- Колонки 1С ---
    TASK_ID_COL = os.getenv("KPP_TASK_ID_COL", "Id")
    TASK_DT_COL = os.getenv("KPP_TASK_DT_COL", "Dt")
    TASK_TAG_COL = os.getenv("KPP_TASK_TAG_COL", "Tag")
    TASK_DOCIDS_COL = os.getenv("KPP_TASK_DOCIDS_COL", "Ids")
    TASK_CREATED_COL = os.getenv("KPP_TASK_CREATED_COL", "")
    TASK_LOAD_MODE = os.getenv("KPP_TASK_LOAD_MODE", "ID_INCREMENTAL").upper()
    TASK_BATCH_SIZE = int(os.getenv("KPP_TASK_BATCH_SIZE", "10000"))
    TASK_LOOKBACK_HOURS = int(os.getenv("KPP_TASK_LOOKBACK_HOURS", "24"))
    TASK_MATCH_WARN_DELTA_HOURS = int(os.getenv("KPP_TASK_MATCH_WARN_DELTA_HOURS", "24"))
    TASK_START_FROM_LATEST_IF_NO_STATE = os.getenv("KPP_TASK_START_FROM_LATEST_IF_NO_STATE", "1") == "1"
    # В live-режиме стартуем с полного кэша 1С, а LOOKBACK используем только для дозагрузки хвоста.
    # Иначе после перезапуска в памяти остаются только последние 24 часа и старые метки перестают матчиться.
    TASK_FULL_CACHE_ON_START = os.getenv("KPP_TASK_FULL_CACHE_ON_START", "1") == "1"
    TASK_EPC_ONLY_MAX_CANDIDATES = int(os.getenv("KPP_TASK_EPC_ONLY_MAX_CANDIDATES", "1"))
    TASK_EPC_ONLY_REQUIRE_TIME_WINDOW = os.getenv("KPP_TASK_EPC_ONLY_REQUIRE_TIME_WINDOW", "1") == "1"

    # --- Склад / Warehouse из 1С ---
    WAREHOUSE_ENABLED = os.getenv("KPP_WAREHOUSE_ENABLED", "1") == "1"
    WAREHOUSE_TABLE = os.getenv("KPP_WAREHOUSE_TABLE", "dbo.Warehouse")
    WAREHOUSE_ID_COL = os.getenv("KPP_WAREHOUSE_ID_COL", "Id")
    WAREHOUSE_DT_COL = os.getenv("KPP_WAREHOUSE_DT_COL", "Dt")
    WAREHOUSE_TAG_COL = os.getenv("KPP_WAREHOUSE_TAG_COL", "Tag")
    WAREHOUSE_DOCIDS_COL = os.getenv("KPP_WAREHOUSE_DOCIDS_COL", "Ids")
    WAREHOUSE_SERIES_COL = os.getenv("KPP_WAREHOUSE_SERIES_COL", "SeriesNumber")
    WAREHOUSE_LOAD_MODE = os.getenv("KPP_WAREHOUSE_LOAD_MODE", "LOOKBACK").upper()
    WAREHOUSE_BATCH_SIZE = int(os.getenv("KPP_WAREHOUSE_BATCH_SIZE", "10000"))
    WAREHOUSE_LOOKBACK_HOURS = int(os.getenv("KPP_WAREHOUSE_LOOKBACK_HOURS", "24"))
    WAREHOUSE_MATCH_WINDOW_HOURS = int(os.getenv("KPP_WAREHOUSE_MATCH_WINDOW_HOURS", "24"))
    WAREHOUSE_RELOAD_INTERVAL_SEC = int(os.getenv("KPP_WAREHOUSE_RELOAD_INTERVAL_SEC", "30"))
    WAREHOUSE_EMIT_INTERVAL_SEC = int(os.getenv("KPP_WAREHOUSE_EMIT_INTERVAL_SEC", "60"))
    WAREHOUSE_ONLY_GRACE_MINUTES = int(os.getenv("KPP_WAREHOUSE_ONLY_GRACE_MINUTES", "10"))
    WAREHOUSE_START_FROM_LATEST_IF_NO_STATE = os.getenv("KPP_WAREHOUSE_START_FROM_LATEST_IF_NO_STATE", "1") == "1"
    WAREHOUSE_FULL_CACHE_ON_START = os.getenv("KPP_WAREHOUSE_FULL_CACHE_ON_START", "1") == "1"
    WAREHOUSE_EPC_ONLY_MAX_CANDIDATES = int(os.getenv("KPP_WAREHOUSE_EPC_ONLY_MAX_CANDIDATES", "1"))
    WAREHOUSE_EPC_ONLY_REQUIRE_TIME_WINDOW = os.getenv("KPP_WAREHOUSE_EPC_ONLY_REQUIRE_TIME_WINDOW", "1") == "1"

    # --- Полная пересборка ---
    FULL_REBUILD_ON_START = os.getenv("KPP_FULL_REBUILD_ON_START", "1") == "1"
    CONTINUE_LIVE_AFTER_REBUILD = os.getenv("KPP_CONTINUE_LIVE_AFTER_REBUILD", "1") == "1"
    REBUILD_RFID_BATCH_SIZE = int(os.getenv("KPP_REBUILD_RFID_BATCH_SIZE", "50000"))
    REBUILD_TASK_BATCH_SIZE = int(os.getenv("KPP_REBUILD_TASK_BATCH_SIZE", "20000"))
    REBUILD_LOG_EVERY_SESSIONS = int(os.getenv("KPP_REBUILD_LOG_EVERY_SESSIONS", "250"))
    REBUILD_SKIP_EXISTING_EVENTS = os.getenv("KPP_REBUILD_SKIP_EXISTING_EVENTS", "0") == "1"

    # --- Основной цикл ---
    POLL_INTERVAL_SEC = float(os.getenv("KPP_POLL_INTERVAL_SEC", "3"))
    TASK_RELOAD_INTERVAL_SEC = int(os.getenv("KPP_TASK_RELOAD_INTERVAL_SEC", "30"))
    ENRICH_INTERVAL_SEC = int(os.getenv("KPP_ENRICH_INTERVAL_SEC", "15"))
    STATUS_INTERVAL_SEC = int(os.getenv("KPP_STATUS_INTERVAL_SEC", "30"))
    RFID_DISAPPEAR_TIMEOUT_SEC = int(os.getenv("KPP_RFID_DISAPPEAR_TIMEOUT_SEC", "35"))
    SESSION_MAX_DURATION_SEC = int(os.getenv("KPP_SESSION_MAX_DURATION_SEC", "900"))

    # --- Окна корреляции ---
    VIDEO_WINDOW_BEFORE_SEC = int(os.getenv("KPP_VIDEO_WINDOW_BEFORE_SEC", "20"))
    VIDEO_WINDOW_AFTER_SEC = int(os.getenv("KPP_VIDEO_WINDOW_AFTER_SEC", "45"))
    SKUD_WINDOW_BEFORE_SEC = int(os.getenv("KPP_SKUD_WINDOW_BEFORE_SEC", "45"))
    SKUD_WINDOW_AFTER_SEC = int(os.getenv("KPP_SKUD_WINDOW_AFTER_SEC", "90"))
    VIDEO_NEAR_SEC = int(os.getenv("KPP_VIDEO_NEAR_SEC", "10"))
    VIDEO_OK_SEC = int(os.getenv("KPP_VIDEO_OK_SEC", "30"))
    SKUD_NEAR_SEC = int(os.getenv("KPP_SKUD_NEAR_SEC", "15"))
    SKUD_OK_SEC = int(os.getenv("KPP_SKUD_OK_SEC", "45"))

    # --- Переобогащение ---
    PENDING_RECHECK_HOURS = int(os.getenv("KPP_PENDING_RECHECK_HOURS", "24"))
    RECHECK_DELAY_SEC = int(os.getenv("KPP_RECHECK_DELAY_SEC", "300"))
    MAX_RECHECK_COUNT = int(os.getenv("KPP_MAX_RECHECK_COUNT", "288"))

    # --- Recovery после рестарта ---
    RECOVERY_LOOKBACK_MINUTES = int(os.getenv("KPP_RECOVERY_LOOKBACK_MINUTES", "20"))
    START_FROM_LATEST_IF_NO_STATE = os.getenv("KPP_START_FROM_LATEST_IF_NO_STATE", "1") == "1"

    # --- Батчи ---
    RFID_BATCH_SIZE = int(os.getenv("KPP_RFID_BATCH_SIZE", "5000"))
    ENRICH_BATCH_SIZE = int(os.getenv("KPP_ENRICH_BATCH_SIZE", "200"))

    # --- Антенны ---
    OUTER_ANTENNAS = {2, 3}
    INNER_ANTENNAS = {1, 4}

    # --- Отчеты ---
    PRINT_REPORTS = os.getenv("KPP_PRINT_REPORTS", "1") == "1"


class Direction(Enum):
    UNKNOWN = "UNKNOWN"
    IN_ = "ВЪЕЗД"
    OUT = "ВЫЕЗД"

    @property
    def db_code(self) -> str:
        if self == Direction.IN_:
            return "IN"
        if self == Direction.OUT:
            return "OUT"
        return "UNKNOWN"


class TransportMode(Enum):
    UNKNOWN = "НЕИЗВЕСТНО"
    ALONE = "САМА"
    HUMAN = "ЧЕЛОВЕК"
    FORKLIFT = "ПОГРУЗЧИК"
    MIXED = "ПОГРУЗЧИК+ЧЕЛОВЕК"


class ConsensusResult(Enum):
    NO_DATA = "НЕТ ДАННЫХ"
    SINGLE = "ОДИН ИСТОЧНИК"
    MAJORITY = "БОЛЬШИНСТВО"
    UNANIMOUS = "ЕДИНОГЛАСНО"
    SPLIT = "РАЗНОГЛАСИЕ"


@dataclass
class Rfid1CTask:
    task_id: Optional[int]
    dt: datetime
    tag: str
    doc_ids: str
    source_row_id: int = 0
    loaded_at: datetime = field(default_factory=datetime.now)
    epc: str = ""
    tid: str = ""

    def __post_init__(self) -> None:
        self.tag = (self.tag or "").upper().strip()
        if len(self.tag) >= 24:
            self.epc = self.tag[:24]
            self.tid = self.tag[24:] or ""


@dataclass
class WarehouseRecord:
    warehouse_id: Optional[int]
    dt: datetime
    tag: str
    doc_ids: str
    series_number: str = ""
    source_row_id: int = 0
    loaded_at: datetime = field(default_factory=datetime.now)
    epc: str = ""
    tid: str = ""

    def __post_init__(self) -> None:
        self.tag = (self.tag or "").upper().strip()
        if len(self.tag) >= 24:
            self.epc = self.tag[:24]
            self.tid = self.tag[24:] or ""


@dataclass
class RfidRead:
    id: int
    record_time: datetime
    antenna: int
    rssi: float
    epc: str
    tid: str

    @property
    def full_tag(self) -> str:
        return ((self.epc or "") + (self.tid or "")).upper()

    @property
    def zone(self) -> str:
        if self.antenna in Config.OUTER_ANTENNAS:
            return "OUTER"
        if self.antenna in Config.INNER_ANTENNAS:
            return "INNER"
        return "UNKNOWN"


@dataclass
class TagSession:
    full_tag: str
    epc: str
    tid: str
    first_seen: datetime
    last_seen: datetime
    reads: List[RfidRead] = field(default_factory=list)
    antenna_counts: Dict[int, int] = field(default_factory=dict)
    zone_counts: Dict[str, int] = field(default_factory=dict)
    event_key_override: str = ""

    def add_read(self, read: RfidRead) -> None:
        self.reads.append(read)
        self.last_seen = read.record_time
        self.antenna_counts[read.antenna] = self.antenna_counts.get(read.antenna, 0) + 1
        self.zone_counts[read.zone] = self.zone_counts.get(read.zone, 0) + 1

    @property
    def distinct_antennas(self) -> List[int]:
        return sorted(self.antenna_counts.keys())

    @property
    def distinct_zones(self) -> List[str]:
        return sorted(z for z, c in self.zone_counts.items() if c > 0)

    @property
    def avg_rssi(self) -> float:
        if not self.reads:
            return 0.0
        return round(sum(r.rssi for r in self.reads) / len(self.reads), 1)

    @property
    def min_rssi(self) -> float:
        return round(min((r.rssi for r in self.reads), default=0.0), 1)

    @property
    def max_rssi(self) -> float:
        return round(max((r.rssi for r in self.reads), default=0.0), 1)

    @property
    def duration_ms(self) -> int:
        return int((self.last_seen - self.first_seen).total_seconds() * 1000)

    @property
    def event_key(self) -> str:
        if self.event_key_override:
            return self.event_key_override
        payload = f"{self.full_tag}|{self.first_seen.isoformat(timespec='milliseconds')}|{self.last_seen.isoformat(timespec='milliseconds')}"
        return hashlib.md5(payload.encode("utf-8")).hexdigest()


@dataclass
class MatchedVideoEvent:
    event_id: int
    event_time: datetime
    direction_code: str
    transport_code: str
    from_camera: int
    to_camera: int
    delta_ms: int
    score: int


@dataclass
class MatchedSkudEvent:
    external_id: int
    event_time: datetime
    direction_code: str
    gate: str
    person: str
    card: str
    delta_ms: int
    score: int


@dataclass
class ScoredDecision:
    direction: Direction
    confidence_pct: int
    consensus: ConsensusResult
    score_in: int
    score_out: int
    source_count: int
    warnings: List[str]


class KPPMonitorReliable:
    def __init__(self) -> None:
        self.tasks_by_full_tag: Dict[str, List[Rfid1CTask]] = {}
        self.tasks_by_epc: Dict[str, List[Rfid1CTask]] = {}
        self.warehouse_by_full_tag: Dict[str, List[WarehouseRecord]] = {}
        self.warehouse_by_epc: Dict[str, List[WarehouseRecord]] = {}
        self.active_sessions: Dict[str, TagSession] = {}
        self.last_rfid_id: int = 0
        self.last_task_row_id: int = 0
        self.last_warehouse_row_id: int = 0
        self.start_time = datetime.now()
        self.last_task_reload_at: Optional[datetime] = None
        self.last_warehouse_reload_at: Optional[datetime] = None
        self.last_warehouse_emit_at: Optional[datetime] = None
        self.last_enrich_at: Optional[datetime] = None
        self.last_status_at: Optional[datetime] = None
        self.saved_reports: int = 0
        self.rebuild_processed_sessions: int = 0
        self.rebuild_processed_reads: int = 0

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    def _conn_kpp(self) -> pyodbc.Connection:
        return pyodbc.connect(Config.KPP_CONN_STR, autocommit=False)

    def _conn_tasks(self) -> pyodbc.Connection:
        return pyodbc.connect(Config.TASK_CONN_STR, autocommit=False)

    def _log(self, message: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        icon = {
            "INFO": "ℹ️",
            "WARN": "⚠️",
            "ERROR": "❌",
            "OK": "✅",
        }.get(level, "ℹ️")
        print(f"[{ts}] {icon} {message}")
        sys.stdout.flush()

    def _state_get(self, key: str) -> Optional[str]:
        query = f"SELECT StateValue FROM {Config.STATE_TABLE} WHERE StateKey = ?"
        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query, key)
                row = cur.fetchone()
                return str(row[0]) if row and row[0] is not None else None
        except Exception as exc:
            self._log(f"Не удалось прочитать state '{key}': {exc}", "WARN")
            return None

    def _state_set(self, key: str, value: str) -> None:
        query = f"""
MERGE {Config.STATE_TABLE} AS tgt
USING (SELECT ? AS StateKey, ? AS StateValue) AS src
ON tgt.StateKey = src.StateKey
WHEN MATCHED THEN
    UPDATE SET StateValue = src.StateValue, UpdatedAt = SYSDATETIME()
WHEN NOT MATCHED THEN
    INSERT (StateKey, StateValue, UpdatedAt)
    VALUES (src.StateKey, src.StateValue, SYSDATETIME());
"""
        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query, key, value)
                conn.commit()
        except Exception as exc:
            self._log(f"Не удалось сохранить state '{key}': {exc}", "WARN")

    def _event_exists(self, event_key: str) -> bool:
        query = f"SELECT TOP (1) 1 FROM {Config.EVENT_TABLE} WHERE EventKey = ?"
        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query, event_key)
                return cur.fetchone() is not None
        except Exception as exc:
            self._log(f"Не удалось проверить существование EventKey={event_key[:12]}: {exc}", "WARN")
            return False

    # ------------------------------------------------------------------
    # Startup / tasks / polling
    # ------------------------------------------------------------------
    def bootstrap_last_rfid_id(self) -> None:
        saved = self._state_get("LAST_RFID_ID")
        if saved and saved.isdigit():
            self.last_rfid_id = int(saved)
            self._log(f"Восстановлен LAST_RFID_ID={self.last_rfid_id}", "OK")
            return

        if not Config.START_FROM_LATEST_IF_NO_STATE:
            self.last_rfid_id = 0
            self._log("State не найден, стартуем с Id=0", "WARN")
            return

        query = f"SELECT ISNULL(MAX(Id), 0) FROM {Config.RFID_TABLE}"
        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query)
                row = cur.fetchone()
                self.last_rfid_id = int(row[0]) if row else 0
                self._log(
                    f"State не найден, стартуем с текущего хвоста RFID: {self.last_rfid_id}",
                    "WARN",
                )
        except Exception as exc:
            self._log(f"Не удалось определить стартовый RFID Id: {exc}", "ERROR")
            self.last_rfid_id = 0

    def _coerce_int(self, value: object, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _coerce_datetime(self, value: object, fallback: Optional[datetime] = None) -> datetime:
        if fallback is None:
            fallback = datetime.now()
        if value is None:
            return fallback
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, dt_time.min)

        text_value = str(value).strip()
        if not text_value:
            return fallback

        try:
            return datetime.fromisoformat(text_value.replace("Z", ""))
        except Exception:
            pass

        formats = [
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y %H:%M",
            "%Y%m%d%H%M%S",
            "%Y-%m-%d",
            "%d.%m.%Y",
            "%d/%m/%Y",
            "%m/%d/%Y",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(text_value, fmt)
            except Exception:
                continue
        return fallback

    def bootstrap_last_task_row_id(self) -> None:
        if Config.TASK_LOAD_MODE != "ID_INCREMENTAL":
            self.last_task_row_id = 0
            return

        saved = self._state_get("LAST_1C_TASK_ROW_ID")
        if saved and saved.isdigit():
            self.last_task_row_id = int(saved)
            self._log(f"Восстановлен LAST_1C_TASK_ROW_ID={self.last_task_row_id}", "OK")
            return

        if not Config.TASK_START_FROM_LATEST_IF_NO_STATE:
            self.last_task_row_id = 0
            self._log("State 1С не найден, стартуем с Id=0", "WARN")
            return

        query = f"SELECT ISNULL(MAX({Config.TASK_ID_COL}), 0) FROM {Config.TASK_TABLE}"
        try:
            with self._conn_tasks() as conn:
                cur = conn.cursor()
                cur.execute(query)
                row = cur.fetchone()
                self.last_task_row_id = self._coerce_int(row[0] if row else 0)
                self._log(
                    f"State 1С не найден, стартуем с текущего хвоста задач: {self.last_task_row_id}",
                    "WARN",
                )
        except Exception as exc:
            self._log(f"Не удалось определить стартовый 1С Id: {exc}", "WARN")
            self.last_task_row_id = 0

    def bootstrap_last_warehouse_row_id(self) -> None:
        if not Config.WAREHOUSE_ENABLED or Config.WAREHOUSE_LOAD_MODE != "ID_INCREMENTAL":
            self.last_warehouse_row_id = 0
            return

        saved = self._state_get("LAST_WAREHOUSE_ROW_ID")
        if saved and saved.isdigit():
            self.last_warehouse_row_id = int(saved)
            self._log(f"Восстановлен LAST_WAREHOUSE_ROW_ID={self.last_warehouse_row_id}", "OK")
            return

        if not Config.WAREHOUSE_START_FROM_LATEST_IF_NO_STATE:
            self.last_warehouse_row_id = 0
            self._log("State склада не найден, стартуем с Id=0", "WARN")
            return

        query = f"SELECT ISNULL(MAX({Config.WAREHOUSE_ID_COL}), 0) FROM {Config.WAREHOUSE_TABLE}"
        try:
            with self._conn_tasks() as conn:
                cur = conn.cursor()
                cur.execute(query)
                row = cur.fetchone()
                self.last_warehouse_row_id = self._coerce_int(row[0] if row else 0)
                self._log(f"State склада не найден, стартуем с текущего хвоста Warehouse: {self.last_warehouse_row_id}", "WARN")
        except Exception as exc:
            self._log(f"Не удалось определить стартовый Warehouse Id: {exc}", "WARN")
            self.last_warehouse_row_id = 0

    def _append_task(self, task: Rfid1CTask) -> bool:
        existing = self.tasks_by_full_tag.setdefault(task.tag, [])
        signature = (task.source_row_id, task.tag, task.doc_ids)
        for item in existing:
            if (item.source_row_id, item.tag, item.doc_ids) == signature:
                return False
        existing.append(task)
        if task.epc:
            self.tasks_by_epc.setdefault(task.epc, []).append(task)
        return True

    def _build_task_from_row(self, row: object) -> Optional[Rfid1CTask]:
        loaded_at = datetime.now()
        source_row_id = self._coerce_int(row[0] if len(row) > 0 else 0)
        task_id_raw = row[0] if len(row) > 0 else None
        dt_value = row[1] if len(row) > 1 else None
        tag_value = str(row[2]) if len(row) > 2 and row[2] is not None else ""
        doc_ids_value = str(row[3]) if len(row) > 3 and row[3] is not None else ""
        tag_value = tag_value.strip().upper()
        if not tag_value:
            return None
        task_dt = self._coerce_datetime(dt_value, fallback=loaded_at)
        task_id: Optional[int] = None
        try:
            task_id = int(task_id_raw)
        except Exception:
            task_id = None
        return Rfid1CTask(
            task_id=task_id,
            dt=task_dt,
            tag=tag_value,
            doc_ids=doc_ids_value,
            source_row_id=source_row_id,
            loaded_at=loaded_at,
        )

    def load_1c_tasks(self) -> int:
        loaded_now = 0
        try:
            with self._conn_tasks() as conn:
                cur = conn.cursor()
                if Config.TASK_LOAD_MODE == "ID_INCREMENTAL":
                    query = f"""
SELECT TOP ({Config.TASK_BATCH_SIZE})
    {Config.TASK_ID_COL},
    {Config.TASK_DT_COL},
    {Config.TASK_TAG_COL},
    {Config.TASK_DOCIDS_COL}
FROM {Config.TASK_TABLE}
WHERE {Config.TASK_ID_COL} > ?
ORDER BY {Config.TASK_ID_COL} ASC;
"""
                    cur.execute(query, self.last_task_row_id)
                else:
                    query = f"""
SELECT TOP ({Config.TASK_BATCH_SIZE})
    {Config.TASK_ID_COL},
    {Config.TASK_DT_COL},
    {Config.TASK_TAG_COL},
    {Config.TASK_DOCIDS_COL}
FROM {Config.TASK_TABLE}
WHERE {Config.TASK_DT_COL} >= DATEADD(hour, -?, GETDATE())
ORDER BY {Config.TASK_DT_COL} DESC, {Config.TASK_ID_COL} DESC;
"""
                    cur.execute(query, Config.TASK_LOOKBACK_HOURS)
                    # ВАЖНО: не очищаем полный кэш. LOOKBACK — это дозагрузка/обновление хвоста,
                    # а не единственный источник 1С-меток. Очистка каждые 30 секунд ломала матчинг
                    # для событий, чьи записи 1С старше окна LOOKBACK.

                max_row_id = self.last_task_row_id
                for row in cur.fetchall():
                    task = self._build_task_from_row(row)
                    if task is None:
                        continue
                    loaded_now += 1 if self._append_task(task) else 0
                    max_row_id = max(max_row_id, task.source_row_id)

                if Config.TASK_LOAD_MODE == "ID_INCREMENTAL" and max_row_id > self.last_task_row_id:
                    self.last_task_row_id = max_row_id
                    self._state_set("LAST_1C_TASK_ROW_ID", str(self.last_task_row_id))

            self.last_task_reload_at = datetime.now()
            total_tasks = sum(len(v) for v in self.tasks_by_full_tag.values())
            self._log(
                f"Задачи 1С загружены: новых={loaded_now}, всего в кэше={total_tasks}, уникальных меток={len(self.tasks_by_full_tag)}, last_task_row_id={self.last_task_row_id}",
                "OK",
            )
            return loaded_now
        except Exception as exc:
            self._log(f"Ошибка загрузки задач 1С: {exc}", "ERROR")
            return 0

    def load_all_1c_tasks_for_rebuild(self) -> int:
        self.tasks_by_full_tag = {}
        self.tasks_by_epc = {}
        total_loaded = 0
        last_id = 0

        self._log("Полная загрузка задач 1С для rebuild...", "INFO")
        try:
            while True:
                query = f"""
SELECT TOP ({Config.REBUILD_TASK_BATCH_SIZE})
    {Config.TASK_ID_COL},
    {Config.TASK_DT_COL},
    {Config.TASK_TAG_COL},
    {Config.TASK_DOCIDS_COL}
FROM {Config.TASK_TABLE}
WHERE {Config.TASK_ID_COL} > ?
ORDER BY {Config.TASK_ID_COL} ASC;
"""
                with self._conn_tasks() as conn:
                    cur = conn.cursor()
                    cur.execute(query, last_id)
                    rows = cur.fetchall()

                if not rows:
                    break

                batch_loaded = 0
                for row in rows:
                    task = self._build_task_from_row(row)
                    if task is None:
                        continue
                    batch_loaded += 1 if self._append_task(task) else 0
                    last_id = max(last_id, task.source_row_id)

                total_loaded += batch_loaded
                self._log(
                    f"1С rebuild batch: loaded={batch_loaded}, total={total_loaded}, last_id={last_id}",
                    "INFO",
                )

            self.last_task_row_id = last_id
            self.last_task_reload_at = datetime.now()
            self._state_set("LAST_1C_TASK_ROW_ID", str(self.last_task_row_id))
            self._log(
                f"Полная загрузка задач 1С завершена: {total_loaded} записей, уникальных меток={len(self.tasks_by_full_tag)}",
                "OK",
            )
            return total_loaded
        except Exception as exc:
            self._log(f"Ошибка полной загрузки задач 1С: {exc}", "ERROR")
            return total_loaded

    def _append_warehouse(self, record: WarehouseRecord) -> bool:
        existing = self.warehouse_by_full_tag.setdefault(record.tag, [])
        signature = (record.source_row_id, record.tag, record.doc_ids, record.series_number)
        for item in existing:
            if (item.source_row_id, item.tag, item.doc_ids, item.series_number) == signature:
                return False
        existing.append(record)
        if record.epc:
            self.warehouse_by_epc.setdefault(record.epc, []).append(record)
        return True

    def _build_warehouse_from_row(self, row: object) -> Optional[WarehouseRecord]:
        loaded_at = datetime.now()
        source_row_id = self._coerce_int(row[0] if len(row) > 0 else 0)
        warehouse_id_raw = row[0] if len(row) > 0 else None
        dt_value = row[1] if len(row) > 1 else None
        tag_value = str(row[2]) if len(row) > 2 and row[2] is not None else ""
        doc_ids_value = str(row[3]) if len(row) > 3 and row[3] is not None else ""
        series_value = str(row[4]) if len(row) > 4 and row[4] is not None else ""
        tag_value = tag_value.strip().upper()
        if not tag_value:
            return None
        warehouse_dt = self._coerce_datetime(dt_value, fallback=loaded_at)
        warehouse_id: Optional[int] = None
        try:
            warehouse_id = int(warehouse_id_raw)
        except Exception:
            warehouse_id = None
        return WarehouseRecord(
            warehouse_id=warehouse_id,
            dt=warehouse_dt,
            tag=tag_value,
            doc_ids=doc_ids_value,
            series_number=series_value,
            source_row_id=source_row_id,
            loaded_at=loaded_at,
        )

    def load_warehouse_records(self) -> int:
        if not Config.WAREHOUSE_ENABLED:
            return 0
        loaded_now = 0
        try:
            with self._conn_tasks() as conn:
                cur = conn.cursor()
                if Config.WAREHOUSE_LOAD_MODE == "ID_INCREMENTAL":
                    query = f"""
SELECT TOP ({Config.WAREHOUSE_BATCH_SIZE})
    {Config.WAREHOUSE_ID_COL},
    {Config.WAREHOUSE_DT_COL},
    {Config.WAREHOUSE_TAG_COL},
    {Config.WAREHOUSE_DOCIDS_COL},
    {Config.WAREHOUSE_SERIES_COL}
FROM {Config.WAREHOUSE_TABLE}
WHERE {Config.WAREHOUSE_ID_COL} > ?
ORDER BY {Config.WAREHOUSE_ID_COL} ASC;
"""
                    cur.execute(query, self.last_warehouse_row_id)
                else:
                    query = f"""
SELECT TOP ({Config.WAREHOUSE_BATCH_SIZE})
    {Config.WAREHOUSE_ID_COL},
    {Config.WAREHOUSE_DT_COL},
    {Config.WAREHOUSE_TAG_COL},
    {Config.WAREHOUSE_DOCIDS_COL},
    {Config.WAREHOUSE_SERIES_COL}
FROM {Config.WAREHOUSE_TABLE}
WHERE {Config.WAREHOUSE_DT_COL} >= DATEADD(hour, -?, GETDATE())
ORDER BY {Config.WAREHOUSE_DT_COL} DESC, {Config.WAREHOUSE_ID_COL} DESC;
"""
                    cur.execute(query, Config.WAREHOUSE_LOOKBACK_HOURS)
                    # Не очищаем полный кэш склада: иначе WAREHOUSE_ONLY/привязка к КПП
                    # видят только последние N часов после каждого reload.

                max_row_id = self.last_warehouse_row_id
                for row in cur.fetchall():
                    record = self._build_warehouse_from_row(row)
                    if record is None:
                        continue
                    loaded_now += 1 if self._append_warehouse(record) else 0
                    max_row_id = max(max_row_id, record.source_row_id)

                if Config.WAREHOUSE_LOAD_MODE == "ID_INCREMENTAL" and max_row_id > self.last_warehouse_row_id:
                    self.last_warehouse_row_id = max_row_id
                    self._state_set("LAST_WAREHOUSE_ROW_ID", str(self.last_warehouse_row_id))

            self.last_warehouse_reload_at = datetime.now()
            total_records = sum(len(v) for v in self.warehouse_by_full_tag.values())
            self._log(
                f"Warehouse загружен: новых={loaded_now}, всего в кэше={total_records}, уникальных меток={len(self.warehouse_by_full_tag)}, last_warehouse_row_id={self.last_warehouse_row_id}",
                "OK",
            )
            return loaded_now
        except Exception as exc:
            self._log(f"Ошибка загрузки Warehouse: {exc}", "ERROR")
            return 0

    def load_all_warehouse_records_for_rebuild(self) -> int:
        if not Config.WAREHOUSE_ENABLED:
            return 0
        self.warehouse_by_full_tag = {}
        self.warehouse_by_epc = {}
        total_loaded = 0
        last_id = 0
        self._log("Полная загрузка Warehouse для rebuild/live-cache...", "INFO")
        try:
            while True:
                query = f"""
SELECT TOP ({Config.WAREHOUSE_BATCH_SIZE})
    {Config.WAREHOUSE_ID_COL},
    {Config.WAREHOUSE_DT_COL},
    {Config.WAREHOUSE_TAG_COL},
    {Config.WAREHOUSE_DOCIDS_COL},
    {Config.WAREHOUSE_SERIES_COL}
FROM {Config.WAREHOUSE_TABLE}
WHERE {Config.WAREHOUSE_ID_COL} > ?
ORDER BY {Config.WAREHOUSE_ID_COL} ASC;
"""
                with self._conn_tasks() as conn:
                    cur = conn.cursor()
                    cur.execute(query, last_id)
                    rows = cur.fetchall()
                if not rows:
                    break
                batch_loaded = 0
                for row in rows:
                    record = self._build_warehouse_from_row(row)
                    if record is None:
                        continue
                    batch_loaded += 1 if self._append_warehouse(record) else 0
                    last_id = max(last_id, record.source_row_id)
                total_loaded += batch_loaded
                self._log(f"Warehouse full-cache batch: loaded={batch_loaded}, total={total_loaded}, last_id={last_id}", "INFO")
            self.last_warehouse_row_id = last_id
            self.last_warehouse_reload_at = datetime.now()
            self._state_set("LAST_WAREHOUSE_ROW_ID", str(self.last_warehouse_row_id))
            self._log(f"Полная загрузка Warehouse завершена: {total_loaded} записей, уникальных меток={len(self.warehouse_by_full_tag)}", "OK")
            return total_loaded
        except Exception as exc:
            self._log(f"Ошибка полной загрузки Warehouse: {exc}", "ERROR")
            return total_loaded

    @staticmethod
    def _candidates_in_time_window(candidates: Iterable[object], event_time: datetime, hours: int) -> List[object]:
        window_sec = hours * 3600
        result: List[object] = []
        for item in candidates:
            dt = getattr(item, "dt", None)
            if dt is None:
                continue
            try:
                if abs((dt - event_time).total_seconds()) <= window_sec:
                    result.append(item)
            except Exception:
                continue
        return result

    def get_new_rfid_reads(self) -> List[RfidRead]:
        query = f"""
SELECT TOP ({Config.RFID_BATCH_SIZE}) Id, RecordTime, Antenna, RSSI, EPC, TID
FROM {Config.RFID_TABLE}
WHERE Id > ?
ORDER BY Id ASC;
"""
        reads: List[RfidRead] = []
        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query, self.last_rfid_id)
                rows = cur.fetchall()
                for row in rows:
                    read = RfidRead(
                        id=int(row[0]),
                        record_time=row[1],
                        antenna=int(row[2]),
                        rssi=float(row[3]) if row[3] is not None else 0.0,
                        epc=(str(row[4]).upper().strip() if row[4] else ""),
                        tid=(str(row[5]).upper().strip() if row[5] else ""),
                    )
                    if not read.epc:
                        continue
                    reads.append(read)
                    self.last_rfid_id = max(self.last_rfid_id, read.id)
            if reads:
                self._log(
                    f"Получено {len(reads)} новых RFID считываний (ID {reads[0].id}-{reads[-1].id})",
                    "INFO",
                )
                self._state_set("LAST_RFID_ID", str(self.last_rfid_id))
            return reads
        except Exception as exc:
            self._log(f"Ошибка чтения RFID: {exc}", "ERROR")
            return []

    # ------------------------------------------------------------------
    # Recovery / sessionization
    # ------------------------------------------------------------------
    def recover_recent_history(self) -> None:
        start_time = datetime.now() - timedelta(minutes=Config.RECOVERY_LOOKBACK_MINUTES)
        query = f"""
SELECT Id, RecordTime, Antenna, RSSI, EPC, TID
FROM {Config.RFID_TABLE}
WHERE RecordTime >= ?
ORDER BY EPC, TID, RecordTime, Id;
"""
        try:
            by_tag: Dict[str, List[RfidRead]] = {}
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query, start_time)
                for row in cur.fetchall():
                    read = RfidRead(
                        id=int(row[0]),
                        record_time=row[1],
                        antenna=int(row[2]),
                        rssi=float(row[3]) if row[3] is not None else 0.0,
                        epc=(str(row[4]).upper().strip() if row[4] else ""),
                        tid=(str(row[5]).upper().strip() if row[5] else ""),
                    )
                    if not read.epc:
                        continue
                    by_tag.setdefault(read.full_tag, []).append(read)

            recovered = 0
            reopened = 0
            now = datetime.now()
            for _, reads in by_tag.items():
                reads.sort(key=lambda r: (r.record_time, r.id))
                for session in self._split_reads_into_sessions(reads):
                    idle_sec = (now - session.last_seen).total_seconds()
                    if idle_sec > Config.RFID_DISAPPEAR_TIMEOUT_SEC:
                        self.persist_session(session, reason="RECOVERY")
                        recovered += 1
                    else:
                        self.active_sessions[session.full_tag] = session
                        reopened += 1
            self._log(
                f"Recovery завершен: восстановлено закрытых={recovered}, активных={reopened}",
                "OK",
            )
        except Exception as exc:
            self._log(f"Ошибка recovery: {exc}", "WARN")

    def _split_reads_into_sessions(self, reads: List[RfidRead]) -> List[TagSession]:
        sessions: List[TagSession] = []
        current: Optional[TagSession] = None
        gap = timedelta(seconds=Config.RFID_DISAPPEAR_TIMEOUT_SEC)
        max_duration = timedelta(seconds=Config.SESSION_MAX_DURATION_SEC)

        for read in reads:
            if current is None:
                current = TagSession(read.full_tag, read.epc, read.tid, read.record_time, read.record_time)
                current.add_read(read)
                continue

            need_new = False
            if read.record_time - current.last_seen > gap:
                need_new = True
            elif read.record_time - current.first_seen > max_duration:
                need_new = True

            if need_new:
                sessions.append(current)
                current = TagSession(read.full_tag, read.epc, read.tid, read.record_time, read.record_time)
                current.add_read(read)
            else:
                current.add_read(read)

        if current is not None:
            sessions.append(current)
        return sessions

    def _build_session_from_reads(self, reads: List[RfidRead]) -> Optional[TagSession]:
        if not reads:
            return None
        reads = sorted(reads, key=lambda r: (r.record_time, r.id))
        session = TagSession(
            full_tag=reads[0].full_tag,
            epc=reads[0].epc,
            tid=reads[0].tid,
            first_seen=reads[0].record_time,
            last_seen=reads[0].record_time,
        )
        for read in reads:
            session.add_read(read)
        session.first_seen = session.reads[0].record_time
        session.last_seen = session.reads[-1].record_time
        return session

    def rebuild_all_history(self) -> None:
        """
        Полностью пересобирает события из всей RFID истории.

        Важно:
        - предполагается, что KPP_ReelEvents уже очищена вручную;
        - если таблицу не очистили, обновление/insert все равно отработает по EventKey;
        - state LAST_RFID_ID после rebuild будет выставлен на max(Id), чтобы live-режим продолжил с хвоста.
        """
        self._log("Запущена полная пересборка KPP_ReelEvents из всей истории RFID...", "WARN")
        self.active_sessions = {}
        self.rebuild_processed_sessions = 0
        self.rebuild_processed_reads = 0

        self.load_all_1c_tasks_for_rebuild()
        self.load_all_warehouse_records_for_rebuild()

        query = f"""
SELECT Id, RecordTime, Antenna, RSSI, EPC, TID
FROM {Config.RFID_TABLE}
ORDER BY EPC ASC, TID ASC, RecordTime ASC, Id ASC;
"""
        current_tag: Optional[str] = None
        current_reads: List[RfidRead] = []
        max_rfid_id = 0

        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query)

                while True:
                    rows = cur.fetchmany(Config.REBUILD_RFID_BATCH_SIZE)
                    if not rows:
                        break

                    for row in rows:
                        read = RfidRead(
                            id=int(row[0]),
                            record_time=row[1],
                            antenna=int(row[2]),
                            rssi=float(row[3]) if row[3] is not None else 0.0,
                            epc=(str(row[4]).upper().strip() if row[4] else ""),
                            tid=(str(row[5]).upper().strip() if row[5] else ""),
                        )
                        if not read.epc:
                            continue

                        self.rebuild_processed_reads += 1
                        max_rfid_id = max(max_rfid_id, read.id)

                        if current_tag is None:
                            current_tag = read.full_tag

                        if read.full_tag != current_tag:
                            self._persist_rebuild_tag_reads(current_reads)
                            current_reads = [read]
                            current_tag = read.full_tag
                        else:
                            current_reads.append(read)

                    self._log(
                        f"Rebuild progress: reads={self.rebuild_processed_reads}, sessions={self.rebuild_processed_sessions}, current_tag={current_tag or '-'}",
                        "INFO",
                    )

            if current_reads:
                self._persist_rebuild_tag_reads(current_reads)

            self.last_rfid_id = max_rfid_id
            self._state_set("LAST_RFID_ID", str(self.last_rfid_id))
            self.last_enrich_at = datetime.now()
            self.reconcile_warehouse_events()

            self._log(
                f"Полная пересборка завершена: reads={self.rebuild_processed_reads}, sessions={self.rebuild_processed_sessions}, last_rfid_id={self.last_rfid_id}",
                "OK",
            )
        except Exception as exc:
            self._log(f"Ошибка полной пересборки истории RFID: {exc}", "ERROR")
            raise

    def _persist_rebuild_tag_reads(self, reads: List[RfidRead]) -> None:
        sessions = self._split_reads_into_sessions(reads)
        for session in sessions:
            if Config.REBUILD_SKIP_EXISTING_EVENTS and self._event_exists(session.event_key):
                continue
            self.persist_session(session, reason="FULL_REBUILD")
            self.rebuild_processed_sessions += 1
            if self.rebuild_processed_sessions % Config.REBUILD_LOG_EVERY_SESSIONS == 0:
                self._log(
                    f"Rebuild sessions committed: {self.rebuild_processed_sessions}",
                    "INFO",
                )

    # ------------------------------------------------------------------
    # Live session handling
    # ------------------------------------------------------------------
    def process_rfid_read(self, read: RfidRead) -> None:
        tag = read.full_tag
        session = self.active_sessions.get(tag)

        if session is None:
            session = TagSession(tag, read.epc, read.tid, read.record_time, read.record_time)
            self.active_sessions[tag] = session
            self._log(f"Новая RFID-сессия: {tag[:32]}... антенна={read.antenna}", "OK")

        session.add_read(read)

    def close_stale_sessions(self) -> int:
        now = datetime.now()
        closed = 0
        for tag in list(self.active_sessions.keys()):
            session = self.active_sessions[tag]
            idle_sec = (now - session.last_seen).total_seconds()
            duration_sec = (session.last_seen - session.first_seen).total_seconds()
            if idle_sec > Config.RFID_DISAPPEAR_TIMEOUT_SEC or duration_sec > Config.SESSION_MAX_DURATION_SEC:
                reason = "TIMEOUT" if idle_sec > Config.RFID_DISAPPEAR_TIMEOUT_SEC else "MAX_DURATION"
                self.persist_session(session, reason=reason)
                del self.active_sessions[tag]
                closed += 1
        return closed

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------
    def _pick_best_task_candidate(self, candidates: List[Rfid1CTask], session: TagSession, warnings: List[str]) -> Optional[Rfid1CTask]:
        if not candidates:
            return None

        delta_limit_sec = Config.TASK_MATCH_WARN_DELTA_HOURS * 3600
        sane: List[Tuple[float, Rfid1CTask]] = []
        fallback: List[Tuple[datetime, int, Rfid1CTask]] = []
        for task in candidates:
            delta_sec = abs((task.dt - session.first_seen).total_seconds())
            if delta_sec <= delta_limit_sec:
                sane.append((delta_sec, task))
            fallback.append((task.loaded_at, task.source_row_id, task))

        if sane:
            sane.sort(key=lambda x: (x[0], -x[1].source_row_id, -x[1].loaded_at.timestamp()))
            return sane[0][1]

        fallback.sort(key=lambda x: (x[0], x[1]), reverse=True)
        warnings.append("Дата 1С далеко от события, выбран самый свежий источник из кэша")
        return fallback[0][2]

    def find_best_task(self, session: TagSession) -> Tuple[Optional[Rfid1CTask], str, List[str]]:
        warnings: List[str] = []

        exact = self.tasks_by_full_tag.get(session.full_tag, [])
        if exact:
            task = self._pick_best_task_candidate(exact, session, warnings)
            if len(exact) > 1:
                warnings.append(f"Несколько exact задач по полной метке: {len(exact)}")
            return task, "FULL_TAG", warnings

        epc_matches = self.tasks_by_epc.get(session.epc, []) if session.epc else []
        if epc_matches:
            event_time = session.first_seen or session.last_seen or datetime.now()
            in_window = self._candidates_in_time_window(epc_matches, event_time, Config.TASK_MATCH_WARN_DELTA_HOURS)
            candidates = in_window if in_window else epc_matches

            if Config.TASK_EPC_ONLY_REQUIRE_TIME_WINDOW and not in_window:
                warnings.append(f"EPC_ONLY запрещен: нет задач 1С в окне ±{Config.TASK_MATCH_WARN_DELTA_HOURS} ч, кандидатов по EPC={len(epc_matches)}")
                return None, "EPC_ONLY_OUT_OF_WINDOW", warnings

            if len(candidates) > Config.TASK_EPC_ONLY_MAX_CANDIDATES:
                warnings.append(f"EPC_ONLY запрещен: неоднозначный EPC, кандидатов 1С={len(candidates)}")
                return None, "EPC_AMBIGUOUS", warnings

            task = self._pick_best_task_candidate(candidates, session, warnings)
            warnings.append("1С: полный EPC+TID не совпал, использован только уникальный EPC")
            return task, "EPC_ONLY_UNIQUE", warnings

        return None, "NOT_FOUND", warnings

    def _pick_best_warehouse_candidate(
        self,
        candidates: List[WarehouseRecord],
        event_time: datetime,
        warnings: List[str],
    ) -> Optional[WarehouseRecord]:
        if not candidates:
            return None
        sane: List[Tuple[float, WarehouseRecord]] = []
        fallback: List[Tuple[datetime, int, WarehouseRecord]] = []
        window_sec = Config.WAREHOUSE_MATCH_WINDOW_HOURS * 3600
        for record in candidates:
            delta_sec = abs((record.dt - event_time).total_seconds())
            if delta_sec <= window_sec:
                sane.append((delta_sec, record))
            fallback.append((record.loaded_at, record.source_row_id, record))
        if sane:
            sane.sort(key=lambda x: (x[0], -x[1].source_row_id, -x[1].loaded_at.timestamp()))
            return sane[0][1]
        fallback.sort(key=lambda x: (x[0], x[1]), reverse=True)
        warnings.append(f"Warehouse: дата склада дальше ±{Config.WAREHOUSE_MATCH_WINDOW_HOURS} ч, выбран самый свежий источник из кэша")
        return fallback[0][2]

    def find_best_warehouse(self, session: TagSession) -> Tuple[Optional[WarehouseRecord], str, List[str]]:
        warnings: List[str] = []
        if not Config.WAREHOUSE_ENABLED:
            return None, "WAREHOUSE_DISABLED", warnings
        event_time = session.last_seen or session.first_seen or datetime.now()
        exact = self.warehouse_by_full_tag.get(session.full_tag, [])
        if exact:
            record = self._pick_best_warehouse_candidate(exact, event_time, warnings)
            if len(exact) > 1:
                warnings.append(f"Несколько Warehouse записей по полной метке: {len(exact)}")
            return record, "WAREHOUSE_FULL_TAG", warnings
        epc_matches = self.warehouse_by_epc.get(session.epc, []) if session.epc else []
        if epc_matches:
            in_window = self._candidates_in_time_window(epc_matches, event_time, Config.WAREHOUSE_MATCH_WINDOW_HOURS)
            candidates = in_window if in_window else epc_matches

            if Config.WAREHOUSE_EPC_ONLY_REQUIRE_TIME_WINDOW and not in_window:
                warnings.append(f"Warehouse EPC_ONLY запрещен: нет склада в окне ±{Config.WAREHOUSE_MATCH_WINDOW_HOURS} ч, кандидатов по EPC={len(epc_matches)}")
                return None, "WAREHOUSE_EPC_ONLY_OUT_OF_WINDOW", warnings

            if len(candidates) > Config.WAREHOUSE_EPC_ONLY_MAX_CANDIDATES:
                warnings.append(f"Warehouse EPC_ONLY запрещен: неоднозначный EPC, кандидатов склада={len(candidates)}")
                return None, "WAREHOUSE_EPC_AMBIGUOUS", warnings

            record = self._pick_best_warehouse_candidate(candidates, event_time, warnings)
            warnings.append("Warehouse: полный EPC+TID не совпал, использован только уникальный EPC")
            return record, "WAREHOUSE_EPC_ONLY_UNIQUE", warnings
        return None, "WAREHOUSE_NOT_FOUND", warnings

    @staticmethod
    def _task_match_with_warehouse(task_match_type: str, warehouse: Optional[WarehouseRecord]) -> str:
        if warehouse is None:
            return task_match_type
        if task_match_type in ("FULL_TAG_WAREHOUSE", "EPC_ONLY_WAREHOUSE", "EPC_ONLY_UNIQUE_WAREHOUSE", "WAREHOUSE_ONLY"):
            return task_match_type
        if task_match_type == "FULL_TAG":
            return "FULL_TAG_WAREHOUSE"
        if task_match_type in ("EPC_ONLY", "EPC_ONLY_UNIQUE"):
            return "EPC_ONLY_UNIQUE_WAREHOUSE"
        return "WAREHOUSE_ONLY"

    def _warehouse_to_evidence(self, warehouse: Optional[WarehouseRecord], match_type: str = "") -> Optional[dict]:
        if warehouse is None:
            return None
        return {
            "warehouse_id": warehouse.warehouse_id,
            "source_row_id": warehouse.source_row_id,
            "dt": warehouse.dt.isoformat(sep=" ", timespec="milliseconds"),
            "tag": warehouse.tag,
            "epc": warehouse.epc,
            "tid": warehouse.tid,
            "doc_ids": warehouse.doc_ids,
            "series_number": warehouse.series_number,
            "match_type": match_type,
        }

    def _warehouse_event_key(self, warehouse: WarehouseRecord) -> str:
        payload = f"WAREHOUSE|{warehouse.source_row_id}|{warehouse.tag}|{warehouse.dt.isoformat(timespec='milliseconds')}"
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    def _find_kpp_event_for_warehouse(self, warehouse: WarehouseRecord) -> Optional[int]:
        exact_query = f"""
SELECT TOP (1) EventId
FROM {Config.EVENT_TABLE}
WHERE SourceTag = ?
  AND ISNULL(SessionCloseReason, '') <> 'WAREHOUSE_ONLY'
  AND FirstSeen BETWEEN DATEADD(hour, -?, ?) AND DATEADD(hour, ?, ?)
ORDER BY ABS(DATEDIFF(second, FirstSeen, ?)) ASC, EventId DESC;
"""
        epc_query = f"""
SELECT TOP (1) EventId
FROM {Config.EVENT_TABLE}
WHERE EPC = ?
  AND ISNULL(SessionCloseReason, '') <> 'WAREHOUSE_ONLY'
  AND FirstSeen BETWEEN DATEADD(hour, -?, ?) AND DATEADD(hour, ?, ?)
ORDER BY ABS(DATEDIFF(second, FirstSeen, ?)) ASC, EventId DESC;
"""
        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(
                    exact_query,
                    warehouse.tag,
                    Config.WAREHOUSE_MATCH_WINDOW_HOURS,
                    warehouse.dt,
                    Config.WAREHOUSE_MATCH_WINDOW_HOURS,
                    warehouse.dt,
                    warehouse.dt,
                )
                row = cur.fetchone()
                if row:
                    return int(row[0])

                # Если считыватель временно пишет только EPC без TID, пытаемся привязать склад
                # к КПП-событию по EPC, но только если EPC не неоднозначен в Warehouse-кэше.
                if warehouse.epc:
                    epc_candidates = self.warehouse_by_epc.get(warehouse.epc, [])
                    if len(epc_candidates) <= Config.WAREHOUSE_EPC_ONLY_MAX_CANDIDATES:
                        cur.execute(
                            epc_query,
                            warehouse.epc,
                            Config.WAREHOUSE_MATCH_WINDOW_HOURS,
                            warehouse.dt,
                            Config.WAREHOUSE_MATCH_WINDOW_HOURS,
                            warehouse.dt,
                            warehouse.dt,
                        )
                        row = cur.fetchone()
                        if row:
                            return int(row[0])
                return None
        except Exception as exc:
            self._log(f"Ошибка поиска КПП-события для Warehouse {warehouse.source_row_id}: {exc}", "WARN")
            return None

    def attach_warehouse_to_existing_event(self, warehouse: WarehouseRecord, event_id: int) -> bool:
        query_select = f"""
SELECT EventId, Task1CId, Task1CDt, Task1CDocIds, TaskMatchType, WarningFlags, EvidenceJson, NeedRecheck
FROM {Config.EVENT_TABLE}
WHERE EventId = ?;
"""
        query_update = f"""
UPDATE {Config.EVENT_TABLE}
SET
    Task1CId = COALESCE(Task1CId, ?),
    Task1CDt = COALESCE(Task1CDt, ?),
    Task1CDocIds = COALESCE(NULLIF(Task1CDocIds, ''), ?),
    TaskMatchType = ?,
    WarningFlags = ?,
    EvidenceJson = ?,
    NeedRecheck = 0,
    NextRecheckAt = NULL,
    FinalizedAt = COALESCE(FinalizedAt, SYSDATETIME()),
    UpdatedAt = SYSDATETIME()
WHERE EventId = ?;
"""
        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query_select, event_id)
                row = cur.fetchone()
                if not row:
                    return False
                task_match = str(row[4] or "NOT_FOUND")
                new_match = self._task_match_with_warehouse(task_match, warehouse)
                warning_flags = str(row[5] or "").strip()
                add_warning = f"Warehouse: катушка зафиксирована на складе {warehouse.dt:%Y-%m-%d %H:%M:%S}"
                if add_warning not in warning_flags:
                    warning_flags = (warning_flags + " | " + add_warning).strip(" |")
                evidence = {}
                try:
                    evidence = json.loads(str(row[6] or "{}"))
                    if not isinstance(evidence, dict):
                        evidence = {"raw": str(row[6])}
                except Exception:
                    evidence = {"raw": str(row[6] or "")}
                existing_warehouse = evidence.get("warehouse") if isinstance(evidence, dict) else None
                if isinstance(existing_warehouse, dict) and str(existing_warehouse.get("source_row_id")) == str(warehouse.source_row_id):
                    return False
                evidence["warehouse"] = self._warehouse_to_evidence(warehouse, "ATTACHED_TO_KPP")
                evidence_json = json.dumps(evidence, ensure_ascii=False, default=str)
                cur.execute(
                    query_update,
                    warehouse.warehouse_id,
                    warehouse.dt,
                    warehouse.doc_ids,
                    new_match,
                    warning_flags,
                    evidence_json,
                    event_id,
                )
                conn.commit()
                return True
        except Exception as exc:
            self._log(f"Ошибка привязки Warehouse {warehouse.source_row_id} к EventId={event_id}: {exc}", "WARN")
            return False

    def upsert_warehouse_only_event(self, warehouse: WarehouseRecord) -> bool:
        task_session = TagSession(
            full_tag=warehouse.tag,
            epc=warehouse.epc,
            tid=warehouse.tid,
            first_seen=warehouse.dt,
            last_seen=warehouse.dt,
            event_key_override=self._warehouse_event_key(warehouse),
        )
        if self._event_exists(task_session.event_key):
            return False
        task, task_match_type, task_warnings = self.find_best_task(task_session)
        match_type = self._task_match_with_warehouse(task_match_type, warehouse)
        warnings = list(task_warnings)
        warnings.append("Warehouse: катушка на складе, проход через КПП не найден")
        decision = ScoredDecision(
            direction=Direction.UNKNOWN,
            confidence_pct=70,
            consensus=ConsensusResult.SINGLE,
            score_in=0,
            score_out=0,
            source_count=1,
            warnings=warnings,
        )
        return self._upsert_event(
            session=task_session,
            reason="WAREHOUSE_ONLY",
            task=task,
            task_match_type=match_type,
            rfid_direction=Direction.UNKNOWN,
            rfid_score=0,
            video=None,
            skud=None,
            warehouse=warehouse,
            warehouse_match_type="WAREHOUSE_ONLY",
            decision=decision,
            need_recheck=0,
            recheck_increment=False,
        )

    def reconcile_warehouse_events(self) -> int:
        if not Config.WAREHOUSE_ENABLED:
            return 0
        now = datetime.now()
        min_age = timedelta(minutes=Config.WAREHOUSE_ONLY_GRACE_MINUTES)
        processed = 0
        records = [item for values in self.warehouse_by_full_tag.values() for item in values]
        records.sort(key=lambda x: (x.dt, x.source_row_id))
        for warehouse in records:
            if now - warehouse.dt < min_age:
                continue
            event_id = self._find_kpp_event_for_warehouse(warehouse)
            if event_id is not None:
                if self.attach_warehouse_to_existing_event(warehouse, event_id):
                    processed += 1
                continue
            if self.upsert_warehouse_only_event(warehouse):
                processed += 1
        if processed:
            self._log(f"Warehouse обработан: привязано/создано событий={processed}", "OK")
        self.last_warehouse_emit_at = datetime.now()
        return processed

    def get_video_events(self, start: datetime, end: datetime) -> List[dict]:
        query = f"""
SELECT Id, Timestamp, Direction, FromCamera, ToCamera, TransportMode
FROM {Config.VIDEO_TABLE}
WHERE Timestamp BETWEEN ? AND ?
ORDER BY Timestamp ASC;
"""
        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query, start, end)
                return [
                    {
                        "id": int(row[0]),
                        "time": row[1],
                        "direction": str(row[2]) if row[2] else "",
                        "from_cam": int(row[3]) if row[3] is not None else None,
                        "to_cam": int(row[4]) if row[4] is not None else None,
                        "transport": str(row[5]) if row[5] else "",
                    }
                    for row in cur.fetchall()
                ]
        except Exception as exc:
            self._log(f"Ошибка чтения видео: {exc}", "WARN")
            return []

    def get_skud_events(self, start: datetime, end: datetime) -> List[dict]:
        query = f"""
SELECT ExternalId2, CreatedAt, Direction, PersonControlDeviceName, FullName, CardNumReal
FROM {Config.SKUD_TABLE}
WHERE CreatedAt BETWEEN ? AND ?
ORDER BY CreatedAt ASC;
"""
        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query, start, end)
                return [
                    {
                        "id": int(row[0]),
                        "time": row[1],
                        "direction": str(row[2]) if row[2] else "",
                        "gate": str(row[3]) if row[3] else "",
                        "person": str(row[4]) if row[4] else "",
                        "card": str(row[5]) if row[5] else "",
                    }
                    for row in cur.fetchall()
                ]
        except Exception as exc:
            self._log(f"Ошибка чтения СКУД: {exc}", "WARN")
            return []

    def match_video(self, session: TagSession) -> Optional[MatchedVideoEvent]:
        mid = session.first_seen + (session.last_seen - session.first_seen) / 2
        events = self.get_video_events(
            session.first_seen - timedelta(seconds=Config.VIDEO_WINDOW_BEFORE_SEC),
            session.last_seen + timedelta(seconds=Config.VIDEO_WINDOW_AFTER_SEC),
        )
        if not events:
            return None

        best: Optional[MatchedVideoEvent] = None
        for event in events:
            delta_ms = int(abs((event["time"] - mid).total_seconds()) * 1000)
            delta_sec = delta_ms / 1000.0
            score = 0
            if delta_sec <= Config.VIDEO_NEAR_SEC:
                score = 4
            elif delta_sec <= Config.VIDEO_OK_SEC:
                score = 3
            elif delta_sec <= max(Config.VIDEO_WINDOW_BEFORE_SEC, Config.VIDEO_WINDOW_AFTER_SEC):
                score = 1
            if score == 0:
                continue
            candidate = MatchedVideoEvent(
                event_id=event["id"],
                event_time=event["time"],
                direction_code=str(event["direction"]),
                transport_code=str(event["transport"]),
                from_camera=event["from_cam"],
                to_camera=event["to_cam"],
                delta_ms=delta_ms,
                score=score,
            )
            if best is None or (candidate.score, -candidate.delta_ms) > (best.score, -best.delta_ms):
                best = candidate
        return best

    def match_skud(self, session: TagSession) -> Optional[MatchedSkudEvent]:
        mid = session.first_seen + (session.last_seen - session.first_seen) / 2
        events = self.get_skud_events(
            session.first_seen - timedelta(seconds=Config.SKUD_WINDOW_BEFORE_SEC),
            session.last_seen + timedelta(seconds=Config.SKUD_WINDOW_AFTER_SEC),
        )
        if not events:
            return None

        nearest = min(events, key=lambda e: abs((e["time"] - mid).total_seconds()))
        same_actor = [
            e
            for e in events
            if e["person"] == nearest["person"] and e["card"] == nearest["card"]
            and abs((e["time"] - nearest["time"]).total_seconds()) <= 7
        ]
        cluster = same_actor or [nearest]

        directions: Dict[str, int] = {}
        for e in cluster:
            code = (e["direction"] or "").upper()
            directions[code] = directions.get(code, 0) + 1
        direction_code = max(directions, key=directions.get) if directions else str(nearest["direction"])

        delta_ms = int(abs((nearest["time"] - mid).total_seconds()) * 1000)
        delta_sec = delta_ms / 1000.0
        score = 0
        if delta_sec <= Config.SKUD_NEAR_SEC:
            score = 3
        elif delta_sec <= Config.SKUD_OK_SEC:
            score = 2
        elif delta_sec <= max(Config.SKUD_WINDOW_BEFORE_SEC, Config.SKUD_WINDOW_AFTER_SEC):
            score = 1
        if score == 0:
            return None

        return MatchedSkudEvent(
            external_id=int(nearest["id"]),
            event_time=nearest["time"],
            direction_code=direction_code,
            gate=str(nearest["gate"]),
            person=str(nearest["person"]),
            card=str(nearest["card"]),
            delta_ms=delta_ms,
            score=score,
        )

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------
    def infer_rfid_direction(self, session: TagSession) -> Tuple[Direction, int, List[str]]:
        warnings: List[str] = []
        if not session.reads:
            return Direction.UNKNOWN, 0, warnings

        ordered = sorted(session.reads, key=lambda r: (r.record_time, r.id))
        compressed: List[str] = []
        for read in ordered:
            zone = read.zone
            if not compressed or compressed[-1] != zone:
                compressed.append(zone)

        has_inner = "INNER" in session.zone_counts
        has_outer = "OUTER" in session.zone_counts
        if not (has_inner and has_outer):
            warnings.append("RFID: только одна зона")
            return Direction.UNKNOWN, 0, warnings

        first_inner = min(r.record_time for r in ordered if r.zone == "INNER")
        first_outer = min(r.record_time for r in ordered if r.zone == "OUTER")
        last_inner = max(r.record_time for r in ordered if r.zone == "INNER")
        last_outer = max(r.record_time for r in ordered if r.zone == "OUTER")

        if first_outer <= first_inner and last_outer >= last_inner:
            return Direction.IN_, 3, warnings
        if first_inner <= first_outer and last_inner >= last_outer:
            return Direction.OUT, 3, warnings

        transitions = list(zip(compressed, compressed[1:]))
        in_votes = sum(1 for a, b in transitions if a == "OUTER" and b == "INNER")
        out_votes = sum(1 for a, b in transitions if a == "INNER" and b == "OUTER")
        if in_votes > out_votes:
            return Direction.IN_, 2, warnings
        if out_votes > in_votes:
            return Direction.OUT, 2, warnings

        warnings.append("RFID: неоднозначная последовательность зон")
        return Direction.UNKNOWN, 0, warnings

    @staticmethod
    def _normalize_video_direction(code: str) -> Direction:
        normalized = (code or "").strip().replace("→", ">")
        if normalized == "0>1":
            return Direction.OUT
        if normalized == "1>0":
            return Direction.IN_
        return Direction.UNKNOWN

    @staticmethod
    def _normalize_skud_direction(code: str) -> Direction:
        normalized = (code or "").strip().upper()
        if normalized == "IN":
            return Direction.IN_
        if normalized == "OUT":
            return Direction.OUT
        return Direction.UNKNOWN

    @staticmethod
    def _normalize_transport(code: str) -> TransportMode:
        text = (code or "").lower()
        if "forklift" in text or "погруз" in text:
            if "human" in text or "человек" in text:
                return TransportMode.MIXED
            return TransportMode.FORKLIFT
        if "human" in text or "человек" in text:
            return TransportMode.HUMAN
        if "alone" in text or "сама" in text:
            return TransportMode.ALONE
        return TransportMode.UNKNOWN

    def build_decision(
        self,
        rfid_direction: Direction,
        rfid_score: int,
        video: Optional[MatchedVideoEvent],
        skud: Optional[MatchedSkudEvent],
        warnings: List[str],
    ) -> ScoredDecision:
        score_in = 0
        score_out = 0
        source_count = 0

        if rfid_direction != Direction.UNKNOWN and rfid_score > 0:
            source_count += 1
            if rfid_direction == Direction.IN_:
                score_in += rfid_score
            else:
                score_out += rfid_score

        if video is not None:
            direction = self._normalize_video_direction(video.direction_code)
            if direction != Direction.UNKNOWN:
                source_count += 1
                if direction == Direction.IN_:
                    score_in += video.score
                else:
                    score_out += video.score
            else:
                warnings.append(f"Видео: неизвестный код направления '{video.direction_code}'")

        if skud is not None:
            direction = self._normalize_skud_direction(skud.direction_code)
            if direction != Direction.UNKNOWN:
                source_count += 1
                if direction == Direction.IN_:
                    score_in += skud.score
                else:
                    score_out += skud.score
            else:
                warnings.append(f"СКУД: неизвестный код направления '{skud.direction_code}'")

        direction = Direction.UNKNOWN
        if score_in > score_out:
            direction = Direction.IN_
        elif score_out > score_in:
            direction = Direction.OUT

        if source_count == 0:
            consensus = ConsensusResult.NO_DATA
        else:
            source_dirs = []
            if rfid_direction != Direction.UNKNOWN and rfid_score > 0:
                source_dirs.append(rfid_direction)
            if video is not None:
                vd = self._normalize_video_direction(video.direction_code)
                if vd != Direction.UNKNOWN:
                    source_dirs.append(vd)
            if skud is not None:
                sd = self._normalize_skud_direction(skud.direction_code)
                if sd != Direction.UNKNOWN:
                    source_dirs.append(sd)
            unique_dirs = set(source_dirs)
            if len(source_dirs) <= 1:
                consensus = ConsensusResult.SINGLE if source_dirs else ConsensusResult.NO_DATA
            elif len(unique_dirs) == 1:
                consensus = ConsensusResult.UNANIMOUS
            elif max(score_in, score_out) >= 2:
                consensus = ConsensusResult.MAJORITY
            else:
                consensus = ConsensusResult.SPLIT

        winner_score = max(score_in, score_out)
        confidence = min(100, int(round((winner_score / 10.0) * 100))) if winner_score > 0 else 0
        if consensus == ConsensusResult.SINGLE and confidence > 70:
            confidence = 70
        if consensus == ConsensusResult.MAJORITY and confidence < 55 and winner_score > 0:
            confidence = 55

        if direction == Direction.UNKNOWN and source_count > 0:
            warnings.append("Есть данные, но направления конфликтуют")

        return ScoredDecision(
            direction=direction,
            confidence_pct=confidence,
            consensus=consensus,
            score_in=score_in,
            score_out=score_out,
            source_count=source_count,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def persist_session(self, session: TagSession, reason: str) -> None:
        task, task_match_type, task_warnings = self.find_best_task(session)
        warehouse, warehouse_match_type, warehouse_warnings = self.find_best_warehouse(session)
        effective_match_type = self._task_match_with_warehouse(task_match_type, warehouse)
        rfid_direction, rfid_score, rfid_warnings = self.infer_rfid_direction(session)
        video = self.match_video(session)
        skud = self.match_skud(session)
        warnings = list(task_warnings) + list(warehouse_warnings) + list(rfid_warnings)
        if warehouse is None and rfid_direction in (Direction.OUT, Direction.UNKNOWN):
            warnings.append(f"Warehouse: ожидается фиксация склада в окне ±{Config.WAREHOUSE_MATCH_WINDOW_HOURS} ч")
        decision = self.build_decision(rfid_direction, rfid_score, video, skud, warnings)

        need_recheck = 1
        if (task is not None or warehouse is not None) and decision.direction != Direction.UNKNOWN and decision.confidence_pct >= 55:
            need_recheck = 0
        if decision.consensus == ConsensusResult.UNANIMOUS and warehouse is not None:
            need_recheck = 0
        if warehouse is None and rfid_direction in (Direction.OUT, Direction.UNKNOWN):
            need_recheck = 1

        ok = self._upsert_event(
            session=session,
            reason=reason,
            task=task,
            task_match_type=effective_match_type,
            rfid_direction=rfid_direction,
            rfid_score=rfid_score,
            video=video,
            skud=skud,
            warehouse=warehouse,
            warehouse_match_type=warehouse_match_type,
            decision=decision,
            need_recheck=need_recheck,
            recheck_increment=False,
        )
        if ok:
            self.saved_reports += 1

        if Config.PRINT_REPORTS:
            self.print_report(session, task, effective_match_type, rfid_direction, rfid_score, video, skud, decision, need_recheck)

    def _upsert_event(
        self,
        session: TagSession,
        reason: str,
        task: Optional[Rfid1CTask],
        task_match_type: str,
        rfid_direction: Direction,
        rfid_score: int,
        video: Optional[MatchedVideoEvent],
        skud: Optional[MatchedSkudEvent],
        warehouse: Optional[WarehouseRecord],
        warehouse_match_type: str,
        decision: ScoredDecision,
        need_recheck: int,
        recheck_increment: bool,
    ) -> bool:
        warnings = list(decision.warnings)
        evidence = {
            "reason": reason,
            "rfid_direction": rfid_direction.db_code,
            "rfid_score": rfid_score,
            "video": None if video is None else {
                "event_id": video.event_id,
                "event_time": video.event_time.isoformat(sep=" ", timespec="milliseconds"),
                "direction_code": video.direction_code,
                "transport_code": video.transport_code,
                "from_camera": video.from_camera,
                "to_camera": video.to_camera,
                "delta_ms": video.delta_ms,
                "score": video.score,
            },
            "skud": None if skud is None else {
                "external_id": skud.external_id,
                "event_time": skud.event_time.isoformat(sep=" ", timespec="milliseconds"),
                "direction_code": skud.direction_code,
                "gate": skud.gate,
                "person": skud.person,
                "card": skud.card,
                "delta_ms": skud.delta_ms,
                "score": skud.score,
            },
            "warehouse": self._warehouse_to_evidence(warehouse, warehouse_match_type),
            "warnings": warnings,
            "antenna_counts": session.antenna_counts,
            "zone_counts": session.zone_counts,
        }

        first_antenna = session.reads[0].antenna if session.reads else None
        last_antenna = session.reads[-1].antenna if session.reads else None
        first_zone = session.reads[0].zone if session.reads else None
        last_zone = session.reads[-1].zone if session.reads else None
        antennas_csv = ",".join(str(a) for a in session.distinct_antennas)
        zones_csv = ",".join(session.distinct_zones)
        completed_at = datetime.now()
        task_id = task.task_id if task else (warehouse.warehouse_id if warehouse else None)
        task_dt = task.dt if task else (warehouse.dt if warehouse else None)
        task_doc = task.doc_ids if task else (warehouse.doc_ids if warehouse else None)
        video_matched = 1 if video is not None else 0
        skud_matched = 1 if skud is not None else 0
        transport_mode = "WAREHOUSE" if reason == "WAREHOUSE_ONLY" else (self._normalize_transport(video.transport_code).name if video else TransportMode.UNKNOWN.name)
        warning_flags = " | ".join(warnings)
        evidence_json = json.dumps(evidence, ensure_ascii=False, default=str)
        next_recheck_at = (datetime.now() + timedelta(seconds=Config.RECHECK_DELAY_SEC)) if need_recheck else None
        recheck_delta = 1 if recheck_increment else 0
        last_recheck_at = datetime.now() if recheck_increment else None
        finalized_at = datetime.now() if not need_recheck else None

        data = {
            "SourceTag": session.full_tag,
            "EPC": session.epc,
            "TID": session.tid,
            "Task1CId": task_id,
            "Task1CDt": task_dt,
            "Task1CDocIds": task_doc,
            "TaskMatchType": task_match_type,
            "FirstSeen": session.first_seen,
            "LastSeen": session.last_seen,
            "CompletedAt": completed_at,
            "SessionCloseReason": reason,
            "RfidReadCount": len(session.reads),
            "DistinctAntennaCount": len(session.distinct_antennas),
            "DistinctZoneCount": len(session.distinct_zones),
            "RfidFirstAntenna": first_antenna,
            "RfidLastAntenna": last_antenna,
            "FirstZone": first_zone,
            "LastZone": last_zone,
            "RfidAntennasCsv": antennas_csv,
            "RfidZonesCsv": zones_csv,
            "AvgRSSI": session.avg_rssi,
            "MinRSSI": session.min_rssi,
            "MaxRSSI": session.max_rssi,
            "DurationMs": session.duration_ms,
            "RfidDirection": rfid_direction.db_code,
            "RfidDirectionScore": rfid_score,
            "VideoMatched": video_matched,
            "VideoEventId": video.event_id if video else None,
            "VideoTime": video.event_time if video else None,
            "VideoDirection": video.direction_code if video else None,
            "VideoTransport": video.transport_code if video else None,
            "VideoTimeDeltaMs": video.delta_ms if video else None,
            "VideoScore": video.score if video else 0,
            "SkudMatched": skud_matched,
            "SkudExternalId": skud.external_id if skud else None,
            "SkudTime": skud.event_time if skud else None,
            "SkudDirection": skud.direction_code if skud else None,
            "SkudGate": skud.gate if skud else None,
            "SkudPerson": skud.person if skud else None,
            "SkudCard": skud.card if skud else None,
            "SkudTimeDeltaMs": skud.delta_ms if skud else None,
            "SkudScore": skud.score if skud else 0,
            "FinalDirection": decision.direction.db_code,
            "ConfidencePct": decision.confidence_pct,
            "ConsensusCode": decision.consensus.name,
            "ScoreIn": decision.score_in,
            "ScoreOut": decision.score_out,
            "SourceCount": decision.source_count,
            "TransportMode": transport_mode,
            "WarningFlags": warning_flags,
            "EvidenceJson": evidence_json,
            "NeedRecheck": need_recheck,
            "NextRecheckAt": next_recheck_at,
            "RecheckCountDelta": recheck_delta,
            "LastRecheckAt": last_recheck_at,
            "FinalizedAt": finalized_at,
        }

        update_query = f"""
UPDATE {Config.EVENT_TABLE}
SET
    SourceTag = ?,
    EPC = ?,
    TID = ?,
    Task1CId = ?,
    Task1CDt = ?,
    Task1CDocIds = ?,
    TaskMatchType = ?,
    FirstSeen = ?,
    LastSeen = ?,
    CompletedAt = ?,
    SessionCloseReason = ?,
    RfidReadCount = ?,
    DistinctAntennaCount = ?,
    DistinctZoneCount = ?,
    RfidFirstAntenna = ?,
    RfidLastAntenna = ?,
    FirstZone = ?,
    LastZone = ?,
    RfidAntennasCsv = ?,
    RfidZonesCsv = ?,
    AvgRSSI = ?,
    MinRSSI = ?,
    MaxRSSI = ?,
    DurationMs = ?,
    RfidDirection = ?,
    RfidDirectionScore = ?,
    VideoMatched = ?,
    VideoEventId = ?,
    VideoTime = ?,
    VideoDirection = ?,
    VideoTransport = ?,
    VideoTimeDeltaMs = ?,
    VideoScore = ?,
    SkudMatched = ?,
    SkudExternalId = ?,
    SkudTime = ?,
    SkudDirection = ?,
    SkudGate = ?,
    SkudPerson = ?,
    SkudCard = ?,
    SkudTimeDeltaMs = ?,
    SkudScore = ?,
    FinalDirection = ?,
    ConfidencePct = ?,
    ConsensusCode = ?,
    ScoreIn = ?,
    ScoreOut = ?,
    SourceCount = ?,
    TransportMode = ?,
    WarningFlags = ?,
    EvidenceJson = ?,
    NeedRecheck = ?,
    NextRecheckAt = ?,
    RecheckCount = ISNULL(RecheckCount, 0) + ?,
    LastRecheckAt = COALESCE(?, LastRecheckAt),
    FinalizedAt = CASE WHEN ? IS NOT NULL THEN ? ELSE FinalizedAt END,
    UpdatedAt = SYSDATETIME()
WHERE EventKey = ?;
"""

        update_params = [
            data["SourceTag"], data["EPC"], data["TID"], data["Task1CId"], data["Task1CDt"],
            data["Task1CDocIds"], data["TaskMatchType"], data["FirstSeen"], data["LastSeen"], data["CompletedAt"],
            data["SessionCloseReason"], data["RfidReadCount"], data["DistinctAntennaCount"], data["DistinctZoneCount"],
            data["RfidFirstAntenna"], data["RfidLastAntenna"], data["FirstZone"], data["LastZone"],
            data["RfidAntennasCsv"], data["RfidZonesCsv"], data["AvgRSSI"], data["MinRSSI"], data["MaxRSSI"],
            data["DurationMs"], data["RfidDirection"], data["RfidDirectionScore"], data["VideoMatched"],
            data["VideoEventId"], data["VideoTime"], data["VideoDirection"], data["VideoTransport"],
            data["VideoTimeDeltaMs"], data["VideoScore"], data["SkudMatched"], data["SkudExternalId"],
            data["SkudTime"], data["SkudDirection"], data["SkudGate"], data["SkudPerson"], data["SkudCard"],
            data["SkudTimeDeltaMs"], data["SkudScore"], data["FinalDirection"], data["ConfidencePct"],
            data["ConsensusCode"], data["ScoreIn"], data["ScoreOut"], data["SourceCount"], data["TransportMode"],
            data["WarningFlags"], data["EvidenceJson"], data["NeedRecheck"], data["NextRecheckAt"],
            data["RecheckCountDelta"], data["LastRecheckAt"], data["FinalizedAt"], data["FinalizedAt"], session.event_key,
        ]

        insert_query = f"""
INSERT INTO {Config.EVENT_TABLE}
(
    EventKey, SourceTag, EPC, TID,
    Task1CId, Task1CDt, Task1CDocIds, TaskMatchType,
    FirstSeen, LastSeen, CompletedAt, SessionCloseReason,
    RfidReadCount, DistinctAntennaCount, DistinctZoneCount,
    RfidFirstAntenna, RfidLastAntenna, FirstZone, LastZone,
    RfidAntennasCsv, RfidZonesCsv,
    AvgRSSI, MinRSSI, MaxRSSI, DurationMs,
    RfidDirection, RfidDirectionScore,
    VideoMatched, VideoEventId, VideoTime, VideoDirection, VideoTransport, VideoTimeDeltaMs, VideoScore,
    SkudMatched, SkudExternalId, SkudTime, SkudDirection, SkudGate, SkudPerson, SkudCard, SkudTimeDeltaMs, SkudScore,
    FinalDirection, ConfidencePct, ConsensusCode, ScoreIn, ScoreOut, SourceCount, TransportMode,
    WarningFlags, EvidenceJson,
    NeedRecheck, NextRecheckAt, RecheckCount, LastRecheckAt,
    CreatedAt, UpdatedAt, FinalizedAt
)
VALUES
(
    ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?,
    ?, ?, ?, ?,
    ?, ?,
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?,
    ?, ?, ?, ?,
    SYSDATETIME(), SYSDATETIME(), ?
);
"""

        insert_params = [
            session.event_key, data["SourceTag"], data["EPC"], data["TID"],
            data["Task1CId"], data["Task1CDt"], data["Task1CDocIds"], data["TaskMatchType"],
            data["FirstSeen"], data["LastSeen"], data["CompletedAt"], data["SessionCloseReason"],
            data["RfidReadCount"], data["DistinctAntennaCount"], data["DistinctZoneCount"],
            data["RfidFirstAntenna"], data["RfidLastAntenna"], data["FirstZone"], data["LastZone"],
            data["RfidAntennasCsv"], data["RfidZonesCsv"],
            data["AvgRSSI"], data["MinRSSI"], data["MaxRSSI"], data["DurationMs"],
            data["RfidDirection"], data["RfidDirectionScore"],
            data["VideoMatched"], data["VideoEventId"], data["VideoTime"], data["VideoDirection"],
            data["VideoTransport"], data["VideoTimeDeltaMs"], data["VideoScore"],
            data["SkudMatched"], data["SkudExternalId"], data["SkudTime"], data["SkudDirection"],
            data["SkudGate"], data["SkudPerson"], data["SkudCard"], data["SkudTimeDeltaMs"], data["SkudScore"],
            data["FinalDirection"], data["ConfidencePct"], data["ConsensusCode"], data["ScoreIn"],
            data["ScoreOut"], data["SourceCount"], data["TransportMode"],
            data["WarningFlags"], data["EvidenceJson"],
            data["NeedRecheck"], data["NextRecheckAt"], data["RecheckCountDelta"], data["LastRecheckAt"],
            data["FinalizedAt"],
        ]

        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(update_query, update_params)
                if cur.rowcount == 0:
                    cur.execute(insert_query, insert_params)
                conn.commit()
                return True
        except Exception as exc:
            self._log(f"Ошибка upsert события {session.event_key[:12]}: {exc}", "ERROR")
            return False

    def _reload_session_reads(self, session: TagSession) -> TagSession:
        margin_ms = 250
        start = session.first_seen - timedelta(milliseconds=margin_ms)
        end = session.last_seen + timedelta(milliseconds=margin_ms)

        if session.tid:
            query = f"""
SELECT Id, RecordTime, Antenna, RSSI, EPC, TID
FROM {Config.RFID_TABLE}
WHERE RecordTime BETWEEN ? AND ?
  AND EPC = ?
  AND TID = ?
ORDER BY RecordTime ASC, Id ASC;
"""
            params = (start, end, session.epc, session.tid)
        else:
            query = f"""
SELECT Id, RecordTime, Antenna, RSSI, EPC, TID
FROM {Config.RFID_TABLE}
WHERE RecordTime BETWEEN ? AND ?
  AND EPC = ?
ORDER BY RecordTime ASC, Id ASC;
"""
            params = (start, end, session.epc)

        restored = TagSession(
            full_tag=session.full_tag,
            epc=session.epc,
            tid=session.tid,
            first_seen=session.first_seen,
            last_seen=session.last_seen,
        )
        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query, params)
                for row in cur.fetchall():
                    read = RfidRead(
                        id=int(row[0]),
                        record_time=row[1],
                        antenna=int(row[2]),
                        rssi=float(row[3]) if row[3] is not None else 0.0,
                        epc=(str(row[4]).upper().strip() if row[4] else ""),
                        tid=(str(row[5]).upper().strip() if row[5] else ""),
                    )
                    restored.add_read(read)
        except Exception as exc:
            self._log(f"Ошибка reload RFID для RECHECK {session.event_key[:12]}: {exc}", "WARN")
            return session

        if restored.reads:
            restored.first_seen = restored.reads[0].record_time
            restored.last_seen = restored.reads[-1].record_time
            return restored
        return session

    def enrich_pending_events(self) -> int:
        query = f"""
SELECT TOP ({Config.ENRICH_BATCH_SIZE})
    EventKey, SourceTag, EPC, TID, FirstSeen, LastSeen,
    ISNULL(RecheckCount, 0), ISNULL(NeedRecheck, 0)
FROM {Config.EVENT_TABLE}
WHERE NeedRecheck = 1
  AND FirstSeen >= DATEADD(hour, -?, GETDATE())
  AND ISNULL(RecheckCount, 0) < ?
  AND (NextRecheckAt IS NULL OR NextRecheckAt <= GETDATE())
ORDER BY ISNULL(NextRecheckAt, FirstSeen) ASC, FirstSeen ASC;
"""
        updated = 0
        try:
            with self._conn_kpp() as conn:
                cur = conn.cursor()
                cur.execute(query, Config.PENDING_RECHECK_HOURS, Config.MAX_RECHECK_COUNT)
                rows = cur.fetchall()

            for row in rows:
                session = TagSession(
                    full_tag=str(row[1]),
                    epc=str(row[2]) if row[2] else "",
                    tid=str(row[3]) if row[3] else "",
                    first_seen=row[4],
                    last_seen=row[5],
                    reads=[],
                )
                session = self._reload_session_reads(session)
                rfid_direction, rfid_score, rfid_warnings = self.infer_rfid_direction(session)
                task, task_match_type, task_warnings = self.find_best_task(session)
                warehouse, warehouse_match_type, warehouse_warnings = self.find_best_warehouse(session)
                effective_match_type = self._task_match_with_warehouse(task_match_type, warehouse)
                video = self.match_video(session)
                skud = self.match_skud(session)
                warnings = list(task_warnings) + list(warehouse_warnings) + list(rfid_warnings)
                if warehouse is None and rfid_direction in (Direction.OUT, Direction.UNKNOWN):
                    warnings.append(f"Warehouse: ожидается фиксация склада в окне ±{Config.WAREHOUSE_MATCH_WINDOW_HOURS} ч")
                decision = self.build_decision(rfid_direction, rfid_score, video, skud, warnings)

                need_recheck = 1
                if (task is not None or warehouse is not None) and decision.direction != Direction.UNKNOWN and decision.confidence_pct >= 55:
                    need_recheck = 0
                if decision.consensus == ConsensusResult.UNANIMOUS and warehouse is not None:
                    need_recheck = 0
                if warehouse is None and rfid_direction in (Direction.OUT, Direction.UNKNOWN):
                    need_recheck = 1
                if int(row[6]) + 1 >= Config.MAX_RECHECK_COUNT:
                    need_recheck = 0

                ok = self._upsert_event(
                    session=session,
                    reason="RECHECK",
                    task=task,
                    task_match_type=effective_match_type,
                    rfid_direction=rfid_direction,
                    rfid_score=rfid_score,
                    video=video,
                    skud=skud,
                    warehouse=warehouse,
                    warehouse_match_type=warehouse_match_type,
                    decision=decision,
                    need_recheck=need_recheck,
                    recheck_increment=True,
                )
                if ok:
                    updated += 1
                    self.saved_reports += 1

            if updated:
                self._log(f"Переобогащено событий: {updated}", "OK")
            self.last_enrich_at = datetime.now()
            return updated
        except Exception as exc:
            self._log(f"Ошибка enrich pending: {exc}", "WARN")
            return 0

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def print_report(
        self,
        session: TagSession,
        task: Optional[Rfid1CTask],
        task_match_type: str,
        rfid_direction: Direction,
        rfid_score: int,
        video: Optional[MatchedVideoEvent],
        skud: Optional[MatchedSkudEvent],
        decision: ScoredDecision,
        need_recheck: int,
    ) -> None:
        print("\n" + "═" * 100)
        print(f"📦 ПЕРЕСЕЧЕНИЕ КПП: {session.event_key[:12]}")
        print("═" * 100)
        print(f"⏰ Первое RFID:       {session.first_seen:%Y-%m-%d %H:%M:%S.%f}"[:-3])
        print(f"⏰ Последнее RFID:    {session.last_seen:%Y-%m-%d %H:%M:%S.%f}"[:-3])
        print(f"🏷️ Метка:             {session.full_tag}")
        print(f"🔑 EPC:               {session.epc}")
        print(f"🔑 TID:               {session.tid}")
        if task:
            print(f"📄 1С ID:             {task.task_id}")
            print(f"📄 1С документ:       {task.doc_ids}")
            print(f"🔗 Тип матчинга 1С:   {task_match_type}")
        else:
            print("📄 1С:                ПОКА НЕТ СОВПАДЕНИЯ")

        print("\n🎯 РЕШЕНИЕ:")
        print(f"   Направление:       {decision.direction.value if decision.direction != Direction.UNKNOWN else 'НЕ ОПРЕДЕЛЕНО'}")
        print(f"   Уверенность:       {decision.confidence_pct}%")
        print(f"   Консенсус:         {decision.consensus.value}")
        print(f"   Баллы IN/OUT:      {decision.score_in}/{decision.score_out}")
        print(f"   Источников:        {decision.source_count}")
        print(f"   Recheck нужен:     {'ДА' if need_recheck else 'НЕТ'}")

        print("\n📡 RFID:")
        print(f"   Считываний:        {len(session.reads)}")
        print(f"   Зоны:              {session.distinct_zones}")
        print(f"   Антенны:           {session.distinct_antennas}")
        print(f"   Avg RSSI:          {session.avg_rssi} dBm")
        print(f"   RFID направление:  {rfid_direction.value if rfid_direction != Direction.UNKNOWN else 'НЕТ'} (score={rfid_score})")

        print("\n📹 ВИДЕО:")
        if video:
            print(f"   Событие:           #{video.event_id}")
            print(f"   Время:             {video.event_time:%Y-%m-%d %H:%M:%S.%f}"[:-3])
            print(f"   Направление:       {video.direction_code}")
            print(f"   Транспорт:         {video.transport_code}")
            print(f"   Δt:                {video.delta_ms} ms")
            print(f"   Score:             {video.score}")
        else:
            print("   Нет подтверждения")

        print("\n🚪 СКУД:")
        if skud:
            print(f"   Событие:           #{skud.external_id}")
            print(f"   Время:             {skud.event_time:%Y-%m-%d %H:%M:%S.%f}"[:-3])
            print(f"   Направление:       {skud.direction_code}")
            print(f"   Лицо/ТС:           {skud.person}")
            print(f"   Ворота:            {skud.gate}")
            print(f"   Δt:                {skud.delta_ms} ms")
            print(f"   Score:             {skud.score}")
        else:
            print("   Нет подтверждения")

        if decision.warnings:
            print("\n⚠️ ПРЕДУПРЕЖДЕНИЯ:")
            for warning in decision.warnings:
                print(f"   - {warning}")
        print("═" * 100)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def maybe_reload_tasks(self) -> None:
        if self.last_task_reload_at is None:
            self.load_1c_tasks()
            return
        if (datetime.now() - self.last_task_reload_at).total_seconds() >= Config.TASK_RELOAD_INTERVAL_SEC:
            self.load_1c_tasks()

    def maybe_enrich_pending(self) -> None:
        if self.last_enrich_at is None:
            self.enrich_pending_events()
            return
        if (datetime.now() - self.last_enrich_at).total_seconds() >= Config.ENRICH_INTERVAL_SEC:
            self.enrich_pending_events()

    def maybe_reload_warehouse(self) -> None:
        if not Config.WAREHOUSE_ENABLED:
            return
        if self.last_warehouse_reload_at is None:
            self.load_warehouse_records()
            return
        if (datetime.now() - self.last_warehouse_reload_at).total_seconds() >= Config.WAREHOUSE_RELOAD_INTERVAL_SEC:
            self.load_warehouse_records()

    def maybe_reconcile_warehouse(self) -> None:
        if not Config.WAREHOUSE_ENABLED:
            return
        if self.last_warehouse_emit_at is None:
            self.reconcile_warehouse_events()
            return
        if (datetime.now() - self.last_warehouse_emit_at).total_seconds() >= Config.WAREHOUSE_EMIT_INTERVAL_SEC:
            self.reconcile_warehouse_events()

    def maybe_print_status(self) -> None:
        if self.last_status_at is None or (datetime.now() - self.last_status_at).total_seconds() >= Config.STATUS_INTERVAL_SEC:
            self.last_status_at = datetime.now()
            self._log(
                f"Статус: задач_1с={sum(len(v) for v in self.tasks_by_full_tag.values())} | warehouse={sum(len(v) for v in self.warehouse_by_full_tag.values())} | уникальных_меток_1с={len(self.tasks_by_full_tag)} | уникальных_меток_склад={len(self.warehouse_by_full_tag)} | last_task_row_id={self.last_task_row_id} | last_warehouse_row_id={self.last_warehouse_row_id} | активных RFID-сессий={len(self.active_sessions)} | last_rfid_id={self.last_rfid_id} | сохранено={self.saved_reports}",
                "INFO",
            )

    def run(self) -> None:
        print("\n" + "█" * 100)
        print("🚀 УМНЫЙ КПП - НАДЕЖНЫЙ МОНИТОРИНГ КАТУШЕК")
        print("█" * 100)
        print(f"⏰ Запуск:                    {self.start_time:%Y-%m-%d %H:%M:%S}")
        print(f"📊 Таймаут пропадания RFID:  {Config.RFID_DISAPPEAR_TIMEOUT_SEC} сек")
        print(f"📊 Интервал опроса:          {Config.POLL_INTERVAL_SEC} сек")
        print(f"📊 Reload задач 1С:          {Config.TASK_RELOAD_INTERVAL_SEC} сек")
        print(f"📊 Режим загрузки 1С:        {Config.TASK_LOAD_MODE}")
        print(f"📊 Полный кэш 1С на старте:  {Config.TASK_FULL_CACHE_ON_START}")
        print(f"📊 Warehouse включен:        {Config.WAREHOUSE_ENABLED}")
        print(f"📊 Режим Warehouse:          {Config.WAREHOUSE_LOAD_MODE}, окно ±{Config.WAREHOUSE_MATCH_WINDOW_HOURS} ч")
        print(f"📊 Полный кэш Warehouse:     {Config.WAREHOUSE_FULL_CACHE_ON_START}")
        print(f"📊 Enrich pending:           {Config.ENRICH_INTERVAL_SEC} сек")
        print(f"📊 Recovery lookback:        {Config.RECOVERY_LOOKBACK_MINUTES} мин")
        print(f"📊 FULL_REBUILD_ON_START:    {Config.FULL_REBUILD_ON_START}")
        print("█" * 100)

        if Config.FULL_REBUILD_ON_START:
            self.rebuild_all_history()
            self.enrich_pending_events()
            self.reconcile_warehouse_events()

            if not Config.CONTINUE_LIVE_AFTER_REBUILD:
                self._log("Полный rebuild завершен. Live-режим отключен настройкой.", "WARN")
                return

            self._log("Переходим в live-режим после полного rebuild.", "OK")
            self.last_status_at = datetime.now()

        else:
            self.bootstrap_last_rfid_id()
            self.bootstrap_last_task_row_id()
            self.bootstrap_last_warehouse_row_id()
            if Config.TASK_FULL_CACHE_ON_START:
                self.load_all_1c_tasks_for_rebuild()
            else:
                self.load_1c_tasks()
            if Config.WAREHOUSE_FULL_CACHE_ON_START:
                self.load_all_warehouse_records_for_rebuild()
            else:
                self.load_warehouse_records()
            self.recover_recent_history()
            self.enrich_pending_events()
            self.reconcile_warehouse_events()
            self.last_status_at = datetime.now()

        try:
            while True:
                self.maybe_reload_tasks()
                self.maybe_reload_warehouse()

                new_reads = self.get_new_rfid_reads()
                for read in new_reads:
                    self.process_rfid_read(read)

                closed = self.close_stale_sessions()
                if closed:
                    self._log(f"Закрыто RFID-сессий: {closed}", "OK")

                self.maybe_enrich_pending()
                self.maybe_reconcile_warehouse()
                self.maybe_print_status()
                time.sleep(Config.POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            self._log("Остановка по Ctrl+C", "WARN")
        except Exception as exc:
            self._log(f"Критическая ошибка: {exc}", "ERROR")
            raise
        finally:
            closed = self.close_stale_sessions()
            if closed:
                self._log(f"Перед выходом закрыто сессий: {closed}", "OK")
            self._state_set("LAST_RFID_ID", str(self.last_rfid_id))
            self._state_set("LAST_1C_TASK_ROW_ID", str(self.last_task_row_id))
            self._state_set("LAST_WAREHOUSE_ROW_ID", str(self.last_warehouse_row_id))


if __name__ == "__main__":
    monitor = KPPMonitorReliable()
    monitor.run()
