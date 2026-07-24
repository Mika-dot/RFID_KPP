#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RFID КПП Aggregator v3.

Ключевые гарантии:
- любой RFID-объект сохраняется, но катушкой считается только подтверждённая
  полной меткой запись 1С/склада (EPC-only по умолчанию запрещён);
- gap/max-duration проверяются до добавления чтения;
- курсор, события и durable active sessions фиксируются одной транзакцией;
- внешний video/SKUD event резервируется одной физической PassageGroup;
- несколько катушек на одном погрузчике учитываются как несколько катушек,
  объединённых одной группой, а не как одно RFID-событие;
- все окна конфигурируемые, 1С строго ±24 часа.

Перед первым запуском выполнить migrations/001_kpp_v3_reliability.sql.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import pyodbc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.kpp_core_v3 import (  # noqa: E402
    Direction,
    ObjectType,
    PassageGroup,
    ReelClassification,
    ReelDecision,
    RegistryRecord,
    RfidRead,
    StrictSessionizer,
    TagSession,
    TimedExternalEvent,
    assign_events_one_to_one,
    classify_reel,
    group_reel_sessions,
    infer_rfid_direction,
    normalize_video_direction,
    session_statistics,
)


logging.basicConfig(
    level=getattr(logging, os.getenv("KPP_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kpp-v3")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


class Config:
    CONN_STR = os.getenv("KPP_CONN_STR", "")
    TASK_CONN_STR = os.getenv("KPP_TASK_CONN_STR", CONN_STR)
    RFID_TABLE = os.getenv("KPP_RFID_TABLE", "dbo.RFID_Tags")
    VIDEO_TABLE = os.getenv("KPP_VIDEO_TABLE", "dbo.ReelTransitions")
    SKUD_TABLE = os.getenv("KPP_SKUD_TABLE", "dbo.RusGuardLogs")
    TASK_TABLE = os.getenv("KPP_TASK_TABLE", "dbo.RfidTags")
    WAREHOUSE_TABLE = os.getenv("KPP_WAREHOUSE_TABLE", "dbo.Warehouse")
    EVENT_TABLE = os.getenv("KPP_EVENT_TABLE", "dbo.KPP_ReelEvents")
    STATE_TABLE = os.getenv("KPP_STATE_TABLE", "dbo.KPP_RuntimeState")
    ACTIVE_TABLE = os.getenv("KPP_ACTIVE_TABLE", "dbo.KPP_ActiveRfidSessions")
    VIDEO_LINK_TABLE = os.getenv("KPP_VIDEO_LINK_TABLE", "dbo.KPP_EventVideoLinks")
    SKUD_LINK_TABLE = os.getenv("KPP_SKUD_LINK_TABLE", "dbo.KPP_EventSkudLinks")

    POLL_SEC = float(os.getenv("KPP_POLL_INTERVAL_SEC", "2"))
    RFID_BATCH_SIZE = int(os.getenv("KPP_RFID_BATCH_SIZE", "5000"))
    GAP_SEC = float(os.getenv("KPP_RFID_DISAPPEAR_TIMEOUT_SEC", "35"))
    MAX_DURATION_SEC = float(os.getenv("KPP_SESSION_MAX_DURATION_SEC", "900"))
    LATE_TOLERANCE_SEC = float(os.getenv("KPP_RFID_LATE_TOLERANCE_SEC", "2"))
    LIVE_SOURCE_LAG_SEC = float(os.getenv("KPP_LIVE_SOURCE_LAG_SEC", "120"))
    BACKLOG_QUIET_CLOSE_SEC = float(os.getenv("KPP_BACKLOG_QUIET_CLOSE_SEC", "300"))
    GROUP_WINDOW_SEC = float(os.getenv("KPP_PASSAGE_GROUP_WINDOW_SEC", "4"))

    TASK_WINDOW_HOURS = float(os.getenv("KPP_TASK_MATCH_WINDOW_HOURS", "24"))
    REGISTRY_CACHE_HOURS = float(os.getenv("KPP_REGISTRY_CACHE_HOURS", "72"))
    ALLOW_UNIQUE_EPC_REEL = env_bool("KPP_ALLOW_UNIQUE_EPC_REEL", False)
    NON_REEL_TAGS_FILE = os.getenv("KPP_NON_REEL_TAGS_FILE", "")

    VIDEO_BEFORE_SEC = float(os.getenv("KPP_VIDEO_WINDOW_BEFORE_SEC", "60"))
    VIDEO_AFTER_SEC = float(os.getenv("KPP_VIDEO_WINDOW_AFTER_SEC", "60"))
    VIDEO_LAG_IN_SEC = float(os.getenv("KPP_VIDEO_EXPECTED_LAG_IN_SEC", "0"))
    VIDEO_LAG_OUT_SEC = float(os.getenv("KPP_VIDEO_EXPECTED_LAG_OUT_SEC", "0"))
    VIDEO_LAG_UNKNOWN_SEC = float(os.getenv("KPP_VIDEO_EXPECTED_LAG_UNKNOWN_SEC", "0"))
    VIDEO_IN_DIRECTION = os.getenv("KPP_VIDEO_IN_DIRECTION", "0>1")

    SKUD_BEFORE_SEC = float(os.getenv("KPP_SKUD_WINDOW_BEFORE_SEC", "90"))
    SKUD_AFTER_SEC = float(os.getenv("KPP_SKUD_WINDOW_AFTER_SEC", "90"))
    SKUD_LAG_IN_SEC = float(os.getenv("KPP_SKUD_EXPECTED_LAG_IN_SEC", "0"))
    SKUD_LAG_OUT_SEC = float(os.getenv("KPP_SKUD_EXPECTED_LAG_OUT_SEC", "0"))
    SKUD_LAG_UNKNOWN_SEC = float(os.getenv("KPP_SKUD_EXPECTED_LAG_UNKNOWN_SEC", "0"))

    OUTER_ANTENNAS = {int(x) for x in os.getenv("KPP_OUTER_ANTENNAS", "2,3").split(",") if x.strip()}
    INNER_ANTENNAS = {int(x) for x in os.getenv("KPP_INNER_ANTENNAS", "1,4").split(",") if x.strip()}

    REGISTRY_RELOAD_SEC = float(os.getenv("KPP_REGISTRY_RELOAD_SEC", "30"))
    RECHECK_SEC = float(os.getenv("KPP_RECHECK_INTERVAL_SEC", "60"))
    UNKNOWN_RECHECK_HOURS = float(os.getenv("KPP_UNKNOWN_RECHECK_HOURS", "30"))
    EXTERNAL_RECHECK_HOURS = float(os.getenv("KPP_EXTERNAL_RECHECK_HOURS", "6"))
    STATUS_SEC = float(os.getenv("KPP_STATUS_INTERVAL_SEC", "10"))
    PROCESSING_VERSION = "3.4.0"
    APP_LOCK_NAME = os.getenv("KPP_APP_LOCK_NAME", "RFID_KPP_AGGREGATOR_V3")


@dataclass
class SessionContext:
    session: TagSession
    decision: ReelDecision
    rfid_direction: Direction


class Aggregator:
    def __init__(self) -> None:
        if not Config.CONN_STR:
            raise RuntimeError("Не задан KPP_CONN_STR")
        self.sessionizer = StrictSessionizer(Config.GAP_SEC, Config.MAX_DURATION_SEC, Config.LATE_TOLERANCE_SEC)
        self.last_rfid_id = 0
        self.tasks_by_full: Dict[str, List[RegistryRecord]] = defaultdict(list)
        self.tasks_by_epc: Dict[str, List[RegistryRecord]] = defaultdict(list)
        self.wh_by_full: Dict[str, List[RegistryRecord]] = defaultdict(list)
        self.wh_by_epc: Dict[str, List[RegistryRecord]] = defaultdict(list)
        self.non_reel_tags = self._load_non_reel_tags()
        self.last_registry_load = datetime.min
        self.last_recheck = datetime.min
        self.last_raw_delivery_at = datetime.now()
        self.lock_conn: Optional[pyodbc.Connection] = None
        self.last_status_at = datetime.min
        self.registry_signature: Optional[Tuple[int, int]] = None
        self.total_reads = 0
        self.total_closed = 0
        self.total_reels = 0
        self.total_unknown = 0
        self.last_event_at: Optional[datetime] = None

    def _load_non_reel_tags(self) -> Set[str]:
        if not Config.NON_REEL_TAGS_FILE:
            return set()
        path = Path(Config.NON_REEL_TAGS_FILE)
        if not path.exists():
            log.warning("Файл non-reel tags не найден: %s", path)
            return set()
        return {
            line.strip().upper()
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

    @staticmethod
    def connect(conn_str: Optional[str] = None) -> pyodbc.Connection:
        return pyodbc.connect(conn_str or Config.CONN_STR, autocommit=False, timeout=15)

    def acquire_singleton(self) -> None:
        self.lock_conn = self.connect()
        cur = self.lock_conn.cursor()
        cur.execute(
            "DECLARE @r int; EXEC @r=sp_getapplock @Resource=?, @LockMode='Exclusive', @LockOwner='Session', @LockTimeout=0; SELECT @r;",
            Config.APP_LOCK_NAME,
        )
        result = int(cur.fetchone()[0])
        if result < 0:
            raise RuntimeError("Другой экземпляр агрегатора уже запущен")
        log.info("Получена singleton-блокировка %s", Config.APP_LOCK_NAME)

    def state_get(self, conn: pyodbc.Connection, key: str) -> Optional[str]:
        cur = conn.cursor()
        cur.execute(f"SELECT StateValue FROM {Config.STATE_TABLE} WHERE StateKey=?", key)
        row = cur.fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def state_set(self, conn: pyodbc.Connection, key: str, value: str) -> None:
        conn.cursor().execute(
            f"""
MERGE {Config.STATE_TABLE} AS t
USING (SELECT ? StateKey, ? StateValue) s ON t.StateKey=s.StateKey
WHEN MATCHED THEN UPDATE SET StateValue=s.StateValue, UpdatedAt=SYSDATETIME()
WHEN NOT MATCHED THEN INSERT(StateKey,StateValue,UpdatedAt) VALUES(s.StateKey,s.StateValue,SYSDATETIME());
""",
            key,
            value,
        )

    def bootstrap(self) -> None:
        self.acquire_singleton()
        with self.connect() as conn:
            saved = self.state_get(conn, "LAST_RFID_ID_V3")
            self.last_rfid_id = int(saved or 0)
            cur = conn.cursor()
            cur.execute(f"SELECT SessionJson FROM {Config.ACTIVE_TABLE}")
            restored: List[TagSession] = []
            for (raw,) in cur.fetchall():
                try:
                    restored.append(TagSession.from_json(str(raw)))
                except Exception as exc:
                    log.error("Не удалось восстановить active session: %s", exc)
            self.sessionizer.restore(restored)
        self.load_registries(force=True)
        log.info("Старт v3.4: LAST_RFID_ID_V3=%s, активных сессий=%s", self.last_rfid_id, len(self.sessionizer.active))
        log.info("Оперативный STATUS выводится каждые %.0f сек даже при отсутствии новых RFID", Config.STATUS_SEC)

    def load_registries(self, force: bool = False) -> None:
        now = datetime.now()
        if not force and (now - self.last_registry_load).total_seconds() < Config.REGISTRY_RELOAD_SEC:
            return
        cutoff = now - timedelta(hours=Config.REGISTRY_CACHE_HOURS)
        tasks_full: Dict[str, List[RegistryRecord]] = defaultdict(list)
        tasks_epc: Dict[str, List[RegistryRecord]] = defaultdict(list)
        wh_full: Dict[str, List[RegistryRecord]] = defaultdict(list)
        wh_epc: Dict[str, List[RegistryRecord]] = defaultdict(list)
        with self.connect(Config.TASK_CONN_STR) as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT Id, Dt, Tag, Ids FROM {Config.TASK_TABLE} WHERE Dt>=?", cutoff)
            for row_id, dt, tag, doc_ids in cur.fetchall():
                full = str(tag or "").strip().upper()
                if not full or dt is None:
                    continue
                rec = RegistryRecord(int(row_id), dt, full, str(doc_ids or ""))
                tasks_full[full].append(rec)
                tasks_epc[rec.epc].append(rec)
            cur.execute(f"SELECT Id, Dt, Tag, Ids, SeriesNumber FROM {Config.WAREHOUSE_TABLE} WHERE Dt>=?", cutoff)
            for row_id, dt, tag, doc_ids, series in cur.fetchall():
                full = str(tag or "").strip().upper()
                if not full or dt is None:
                    continue
                rec = RegistryRecord(int(row_id), dt, full, str(doc_ids or ""), str(series or ""))
                wh_full[full].append(rec)
                wh_epc[rec.epc].append(rec)
        self.tasks_by_full, self.tasks_by_epc = tasks_full, tasks_epc
        self.wh_by_full, self.wh_by_epc = wh_full, wh_epc
        self.last_registry_load = now
        signature = (len(tasks_full), len(wh_full))
        if force or signature != self.registry_signature:
            log.info("Справочники обновлены: 1С=%s меток, склад=%s меток, окно=%sч", len(tasks_full), len(wh_full), Config.REGISTRY_CACHE_HOURS)
        self.registry_signature = signature

    def fetch_reads(self, conn: pyodbc.Connection) -> Tuple[List[RfidRead], int, List[Tuple[int, str]]]:
        """Читает пакет и возвращает также watermark всех строк, включая битые.

        Битая строка не должна навсегда остановить cursor на одном Id. Она
        фиксируется в KPP_ProcessingErrors, после чего watermark можно безопасно
        продвинуть в той же транзакции.
        """
        cur = conn.cursor()
        cur.execute(
            f"""
SELECT TOP ({Config.RFID_BATCH_SIZE})
       Id, COALESCE(SourceReaderTime,RecordTime), Antenna, RSSI, EPC, TID,
       ReceivedAt, COALESCE(TimeQuality,'LEGACY_RECORD_TIME'),
       CONVERT(varchar(36),IngestBatchId)
FROM {Config.RFID_TABLE}
WHERE Id>?
ORDER BY Id ASC;
""",
            self.last_rfid_id,
        )
        rows = cur.fetchall()
        batch_max_id = max([self.last_rfid_id] + [int(r[0]) for r in rows])
        result: List[RfidRead] = []
        errors: List[Tuple[int, str]] = []
        for row in rows:
            row_id = int(row[0])
            epc = str(row[4] or "").strip().upper()
            if not epc:
                errors.append((row_id, "EMPTY_EPC"))
                continue
            if row[1] is None:
                errors.append((row_id, "MISSING_EVENT_TIME"))
                continue
            try:
                result.append(
                    RfidRead(
                        id=row_id,
                        record_time=row[1],
                        antenna=int(row[2] or 0),
                        rssi=float(row[3] or 0),
                        epc=epc,
                        tid=str(row[5] or "").strip().upper(),
                        received_at=row[6],
                        time_quality=str(row[7] or "UNKNOWN"),
                        ingest_batch_id=str(row[8]) if row[8] else None,
                    )
                )
            except Exception as exc:
                errors.append((row_id, f"RFID_ROW_PARSE_ERROR: {exc}"))
        return result, batch_max_id, errors

    @staticmethod
    def record_processing_errors(conn: pyodbc.Connection, errors: Sequence[Tuple[int, str]]) -> None:
        for row_id, error in errors:
            conn.cursor().execute(
                """
IF NOT EXISTS(
  SELECT 1 FROM dbo.KPP_ProcessingErrors
  WHERE ServiceName='kpp_aggregator_v3' AND SourceKey=? AND ResolvedAt IS NULL
)
 INSERT INTO dbo.KPP_ProcessingErrors(ServiceName,SourceKey,ErrorText,PayloadJson)
 VALUES('kpp_aggregator_v3',?,?,?);
""",
                str(row_id),
                str(row_id),
                error[:2000],
                json.dumps({"RFID_Tags.Id": row_id}, ensure_ascii=False),
            )

    def snapshot_active(self) -> List[str]:
        return [session.to_json() for session in self.sessionizer.active.values()]

    def restore_active_snapshot(self, snapshot: Sequence[str]) -> None:
        replacement = StrictSessionizer(Config.GAP_SEC, Config.MAX_DURATION_SEC, Config.LATE_TOLERANCE_SEC)
        replacement.restore(TagSession.from_json(raw) for raw in snapshot)
        self.sessionizer = replacement

    @staticmethod
    def build_recheck_session(
        source_tag: str, rows: Sequence[Sequence[object]], close_reason: Optional[str] = None
    ) -> Optional[TagSession]:
        """Восстанавливает только строки конкретной метки из raw ID range.

        Между min/max ID одной сессии находятся чтения других RFID-объектов;
        добавлять их в recheck-сессию нельзя.
        """
        expected = str(source_tag or "").strip().upper()
        reads: List[RfidRead] = []
        for r in rows:
            if r[1] is None or not r[4]:
                continue
            item = RfidRead(
                int(r[0]), r[1], int(r[2] or 0), float(r[3] or 0),
                str(r[4] or "").strip().upper(), str(r[5] or "").strip().upper(),
                r[6], str(r[7] or "UNKNOWN"), str(r[8]) if r[8] else None,
            )
            if item.full_tag == expected:
                reads.append(item)
        if not reads:
            return None
        reads.sort(key=lambda x: (x.record_time, x.id))
        session = TagSession(expected, reads[0].epc, reads[0].tid, reads[0].record_time, reads[0].record_time)
        for read in reads:
            session.add(read)
        session.close_reason = close_reason or "RECHECK"
        return session

    def classify(self, session: TagSession) -> ReelDecision:
        return classify_reel(
            session,
            self.tasks_by_full,
            self.wh_by_full,
            self.tasks_by_epc,
            self.wh_by_epc,
            window_hours=Config.TASK_WINDOW_HOURS,
            allow_unique_epc=Config.ALLOW_UNIQUE_EPC_REEL,
            explicit_non_reel_tags=self.non_reel_tags,
        )

    def fetch_video_events(self, conn: pyodbc.Connection, groups: Sequence[PassageGroup]) -> List[TimedExternalEvent]:
        if not groups:
            return []
        start = min(g.anchor_time for g in groups) - timedelta(seconds=Config.VIDEO_BEFORE_SEC + 10)
        end = max(g.anchor_time for g in groups) + timedelta(seconds=Config.VIDEO_AFTER_SEC + 10)
        cur = conn.cursor()
        cur.execute(
            f"""
SELECT Id, COALESCE(CapturedAt,[Timestamp]), Direction, TransportMode,
       ReelCount, CONVERT(varchar(36),ClientEventUuid)
FROM {Config.VIDEO_TABLE}
WHERE COALESCE(CapturedAt,[Timestamp]) BETWEEN ? AND ?
ORDER BY COALESCE(CapturedAt,[Timestamp]), Id;
""",
            start,
            end,
        )
        return [
            TimedExternalEvent(
                id=int(r[0]),
                event_time=r[1],
                direction=normalize_video_direction(r[2], Config.VIDEO_IN_DIRECTION),
                transport=str(r[3] or "UNKNOWN"),
                reel_count=int(r[4]) if r[4] is not None else None,
                client_uuid=str(r[5]) if r[5] else None,
            )
            for r in cur.fetchall()
            if r[1] is not None
        ]

    def fetch_skud_events(self, conn: pyodbc.Connection, groups: Sequence[PassageGroup]) -> List[TimedExternalEvent]:
        if not groups:
            return []
        start = min(g.anchor_time for g in groups) - timedelta(seconds=Config.SKUD_BEFORE_SEC + 10)
        end = max(g.anchor_time for g in groups) + timedelta(seconds=Config.SKUD_AFTER_SEC + 10)
        cur = conn.cursor()
        cur.execute(
            f"""
SELECT ExternalId2, CreatedAt, Direction, FullName, CardNumReal, PersonControlDeviceName
FROM {Config.SKUD_TABLE}
WHERE CreatedAt BETWEEN ? AND ?
ORDER BY CreatedAt, ExternalId2;
""",
            start,
            end,
        )
        events: List[TimedExternalEvent] = []
        for r in cur.fetchall():
            direction = Direction.IN if str(r[2] or "").upper() == "IN" else Direction.OUT if str(r[2] or "").upper() == "OUT" else Direction.UNKNOWN
            events.append(
                TimedExternalEvent(
                    id=int(r[0]),
                    event_time=r[1],
                    direction=direction,
                    payload={"person": str(r[3] or ""), "card": str(r[4] or ""), "gate": str(r[5] or "")},
                )
            )
        return events

    @staticmethod
    def reserved_ids(conn: pyodbc.Connection, table: str, id_col: str, start: datetime, end: datetime) -> Set[int]:
        # Link-таблицы маленькие; диапазон времени хранится в исходном событии, поэтому здесь читаем ID целиком.
        cur = conn.cursor()
        cur.execute(f"SELECT {id_col} FROM {table}")
        return {int(r[0]) for r in cur.fetchall()}

    def existing_video_assignments(
        self, conn: pyodbc.Connection, groups: Sequence[PassageGroup]
    ) -> Dict[str, TimedExternalEvent]:
        result: Dict[str, TimedExternalEvent] = {}
        keys = [g.group_key for g in groups]
        for pos in range(0, len(keys), 500):
            chunk = keys[pos:pos + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            cur = conn.cursor()
            cur.execute(
                f"""
SELECT l.PassageGroupKey,v.Id,COALESCE(v.CapturedAt,v.[Timestamp]),v.Direction,
       v.TransportMode,v.ReelCount,CONVERT(varchar(36),v.ClientEventUuid)
FROM {Config.VIDEO_LINK_TABLE} l
JOIN {Config.VIDEO_TABLE} v ON v.Id=l.VideoEventId
WHERE l.PassageGroupKey IN ({placeholders});
""",
                chunk,
            )
            for row in cur.fetchall():
                result[str(row[0])] = TimedExternalEvent(
                    id=int(row[1]),
                    event_time=row[2],
                    direction=normalize_video_direction(row[3], Config.VIDEO_IN_DIRECTION),
                    transport=str(row[4] or "UNKNOWN"),
                    reel_count=int(row[5]) if row[5] is not None else None,
                    client_uuid=str(row[6]) if row[6] else None,
                )
        return result

    def existing_skud_assignments(
        self, conn: pyodbc.Connection, groups: Sequence[PassageGroup]
    ) -> Dict[str, TimedExternalEvent]:
        result: Dict[str, TimedExternalEvent] = {}
        keys = [g.group_key for g in groups]
        for pos in range(0, len(keys), 500):
            chunk = keys[pos:pos + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            cur = conn.cursor()
            cur.execute(
                f"""
SELECT l.PassageGroupKey,s.ExternalId2,s.CreatedAt,s.Direction,s.FullName,s.CardNumReal,s.PersonControlDeviceName
FROM {Config.SKUD_LINK_TABLE} l
JOIN {Config.SKUD_TABLE} s ON s.ExternalId2=l.SkudExternalId
WHERE l.PassageGroupKey IN ({placeholders});
""",
                chunk,
            )
            for row in cur.fetchall():
                raw_direction = str(row[3] or "").upper()
                direction = Direction.IN if raw_direction == "IN" else Direction.OUT if raw_direction == "OUT" else Direction.UNKNOWN
                result[str(row[0])] = TimedExternalEvent(
                    id=int(row[1]),
                    event_time=row[2],
                    direction=direction,
                    payload={"person": str(row[4] or ""), "card": str(row[5] or ""), "gate": str(row[6] or "")},
                )
        return result

    def existing_event_group_keys(
        self, conn: pyodbc.Connection, sessions: Sequence[TagSession]
    ) -> Dict[str, str]:
        result: Dict[str, str] = {}
        keys = [s.event_key for s in sessions]
        for pos in range(0, len(keys), 500):
            chunk = keys[pos:pos + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            cur = conn.cursor()
            cur.execute(
                f"SELECT EventKey,PassageGroupKey FROM {Config.EVENT_TABLE} WHERE EventKey IN ({placeholders}) AND PassageGroupKey IS NOT NULL",
                chunk,
            )
            result.update({str(r[0]): str(r[1]) for r in cur.fetchall() if r[1]})
        return result

    @staticmethod
    def preserve_passage_group_keys(
        groups: Sequence[PassageGroup],
        existing_keys: Mapping[str, str],
        directions: Mapping[str, Direction],
    ) -> List[PassageGroup]:
        """Не меняет group key при позднем recheck и не склеивает старые группы."""
        result: List[PassageGroup] = []
        for group in groups:
            buckets: Dict[str, List[TagSession]] = defaultdict(list)
            unassigned: List[TagSession] = []
            for session in group.sessions:
                key = existing_keys.get(session.event_key)
                if key:
                    buckets[key].append(session)
                else:
                    unassigned.append(session)
            if not buckets:
                result.append(group)
                continue
            if len(buckets) == 1:
                only_key = next(iter(buckets))
                buckets[only_key].extend(unassigned)
            else:
                # Если свежая кластеризация пересекла две уже существующие группы,
                # новые сессии присоединяются к ближайшей, старые ключи не меняются.
                for session in unassigned:
                    target_key = min(
                        buckets,
                        key=lambda key: abs(
                            session.midpoint.timestamp()
                            - sum(x.midpoint.timestamp() for x in buckets[key]) / len(buckets[key])
                        ),
                    )
                    buckets[target_key].append(session)
            for key, members in sorted(buckets.items()):
                known = [directions.get(x.event_key, Direction.UNKNOWN) for x in members]
                known = [x for x in known if x != Direction.UNKNOWN]
                direction = max((Direction.IN, Direction.OUT), key=lambda d: known.count(d)) if known else Direction.UNKNOWN
                anchor = sum(x.midpoint.timestamp() for x in members) / len(members)
                result.append(PassageGroup(members, direction, datetime.fromtimestamp(anchor, tz=members[0].midpoint.tzinfo), key))
        result.sort(key=lambda g: (g.anchor_time, g.group_key))
        return result

    def build_contexts(self, sessions: Sequence[TagSession]) -> List[SessionContext]:
        return [
            SessionContext(
                session=s,
                decision=self.classify(s),
                rfid_direction=infer_rfid_direction(s, Config.OUTER_ANTENNAS, Config.INNER_ANTENNAS),
            )
            for s in sessions
        ]

    def persist_sessions(self, conn: pyodbc.Connection, sessions: Sequence[TagSession]) -> None:
        if not sessions:
            return
        contexts = self.build_contexts(sessions)
        reel_contexts = [c for c in contexts if c.decision.is_reel]
        directions = {c.session.event_key: c.rfid_direction for c in reel_contexts}
        groups = group_reel_sessions([c.session for c in reel_contexts], directions, Config.GROUP_WINDOW_SEC)
        existing_group_keys = self.existing_event_group_keys(conn, [c.session for c in reel_contexts])
        groups = self.preserve_passage_group_keys(groups, existing_group_keys, directions)

        videos = self.fetch_video_events(conn, groups)
        skuds = self.fetch_skud_events(conn, groups)
        existing_video = self.existing_video_assignments(conn, groups)
        existing_skud = self.existing_skud_assignments(conn, groups)
        reserved_video = self.reserved_ids(conn, Config.VIDEO_LINK_TABLE, "VideoEventId", datetime.min, datetime.max)
        reserved_skud = self.reserved_ids(conn, Config.SKUD_LINK_TABLE, "SkudExternalId", datetime.min, datetime.max)
        # Миграция не переписывает старую историю. Уже использованные legacy ID
        # также резервируются, чтобы v3 не присвоил их новым passage groups.
        cur = conn.cursor()
        cur.execute(f"SELECT DISTINCT VideoEventId FROM {Config.EVENT_TABLE} WHERE VideoEventId IS NOT NULL")
        reserved_video.update(int(r[0]) for r in cur.fetchall())
        cur.execute(f"SELECT DISTINCT SkudExternalId FROM {Config.EVENT_TABLE} WHERE SkudExternalId IS NOT NULL")
        reserved_skud.update(int(r[0]) for r in cur.fetchall())
        unlinked_video_groups = [g for g in groups if g.group_key not in existing_video]
        unlinked_skud_groups = [g for g in groups if g.group_key not in existing_skud]
        video_assignment = dict(existing_video)
        video_assignment.update(assign_events_one_to_one(
            unlinked_video_groups,
            videos,
            {
                Direction.IN: Config.VIDEO_LAG_IN_SEC,
                Direction.OUT: Config.VIDEO_LAG_OUT_SEC,
                Direction.UNKNOWN: Config.VIDEO_LAG_UNKNOWN_SEC,
            },
            Config.VIDEO_BEFORE_SEC,
            Config.VIDEO_AFTER_SEC,
            reserved_video,
        ))
        skud_assignment = dict(existing_skud)
        skud_assignment.update(assign_events_one_to_one(
            unlinked_skud_groups,
            skuds,
            {
                Direction.IN: Config.SKUD_LAG_IN_SEC,
                Direction.OUT: Config.SKUD_LAG_OUT_SEC,
                Direction.UNKNOWN: Config.SKUD_LAG_UNKNOWN_SEC,
            },
            Config.SKUD_BEFORE_SEC,
            Config.SKUD_AFTER_SEC,
            reserved_skud,
        ))

        group_by_event: Dict[str, PassageGroup] = {}
        for group in groups:
            group.video = video_assignment.get(group.group_key)
            group.skud = skud_assignment.get(group.group_key)
            if group.video and group.group_key not in existing_video:
                conn.cursor().execute(
                    f"INSERT INTO {Config.VIDEO_LINK_TABLE}(PassageGroupKey,VideoEventId,ReelCount) VALUES(?,?,?)",
                    group.group_key,
                    group.video.id,
                    group.reel_count,
                )
            elif group.video:
                conn.cursor().execute(
                    f"UPDATE {Config.VIDEO_LINK_TABLE} SET ReelCount=? WHERE PassageGroupKey=?",
                    group.reel_count,
                    group.group_key,
                )
            if group.skud and group.group_key not in existing_skud:
                conn.cursor().execute(
                    f"INSERT INTO {Config.SKUD_LINK_TABLE}(PassageGroupKey,SkudExternalId) VALUES(?,?)",
                    group.group_key,
                    group.skud.id,
                )
            for session in group.sessions:
                group_by_event[session.event_key] = group

        for context in contexts:
            group = group_by_event.get(context.session.event_key)
            self.upsert_event(conn, context, group)
            s = context.session
            d = context.decision
            if d.is_reel:
                self.total_reels += 1
            else:
                self.total_unknown += 1
            self.last_event_at = datetime.now()
            log.info(
                "СЕССИЯ %s tag=%s raw=%s-%s reads=%s time=%s..%s duration=%.1fs dir=%s video=%s skud=%s group_reels=%s reason=%s",
                "КАТУШКА" if d.is_reel else "RFID-ОБЪЕКТ НЕ ПОДТВЕРЖДЕН КАК КАТУШКА",
                s.full_tag[:48], s.raw_id_min, s.raw_id_max, len(s.reads),
                s.first_seen.strftime("%H:%M:%S.%f")[:-3], s.last_seen.strftime("%H:%M:%S.%f")[:-3],
                s.duration_sec, context.rfid_direction.value,
                group.video.id if group and group.video else "-",
                group.skud.id if group and group.skud else "-",
                group.reel_count if group else "-", s.close_reason or "TIMEOUT",
            )

    def upsert_event(self, conn: pyodbc.Connection, context: SessionContext, group: Optional[PassageGroup]) -> None:
        s, d = context.session, context.decision
        stats = session_statistics(s, Config.OUTER_ANTENNAS, Config.INNER_ANTENNAS)
        video = group.video if group else None
        skud = group.skud if group else None

        weighted_sources: List[Tuple[Direction, int, str]] = [(context.rfid_direction, 4, "RFID")]
        if video:
            weighted_sources.append((video.direction, 4, "VIDEO"))
        if skud:
            weighted_sources.append((skud.direction, 3, "SKUD"))
        known = [direction for direction, _weight, _name in weighted_sources if direction != Direction.UNKNOWN]
        score_in = sum(weight for direction, weight, _name in weighted_sources if direction == Direction.IN)
        score_out = sum(weight for direction, weight, _name in weighted_sources if direction == Direction.OUT)
        if score_in == score_out and score_in > 0:
            final_direction = Direction.UNKNOWN
            consensus = "CONFLICT"
            confidence = 0
        elif score_in > score_out:
            final_direction = Direction.IN
            consensus = "UNANIMOUS" if score_out == 0 and len(known) >= 2 else "WEIGHTED_MAJORITY" if len(known) >= 2 else "SINGLE"
            confidence = 90 if consensus == "UNANIMOUS" else 70 if len(known) >= 2 else 55
        elif score_out > score_in:
            final_direction = Direction.OUT
            consensus = "UNANIMOUS" if score_in == 0 and len(known) >= 2 else "WEIGHTED_MAJORITY" if len(known) >= 2 else "SINGLE"
            confidence = 90 if consensus == "UNANIMOUS" else 70 if len(known) >= 2 else 55
        else:
            final_direction = Direction.UNKNOWN
            consensus = "NO_DATA"
            confidence = 0
        warnings = list(s.warning_flags) + list(d.warnings)
        if consensus == "CONFLICT":
            warnings.append("DIRECTION_CONFLICT")
        if d.is_reel and not video:
            warnings.append("VIDEO_NOT_MATCHED")
        if d.is_reel and not skud:
            warnings.append("SKUD_NOT_MATCHED")
        if s.duration_sec > Config.MAX_DURATION_SEC + 0.001:
            warnings.append("SESSION_DURATION_INVARIANT_VIOLATION")

        task = d.task
        warehouse = d.warehouse
        task_match_type = d.classification.value if d.is_reel else "NOT_REEL"
        group_key = group.group_key if group else None
        group_count = group.reel_count if group else None
        qualities = {str(r.time_quality or "UNKNOWN").upper() for r in s.reads}
        if any("APPROXIMATE" in q or "UNKNOWN" in q for q in qualities):
            time_quality = "APPROXIMATE"
        elif any("LEGACY" in q for q in qualities):
            time_quality = "LEGACY_RECORD_TIME"
        elif any("HOST" in q for q in qualities):
            time_quality = "HOST_CAPTURE_TIME"
        else:
            time_quality = "SOURCE_TIME"
        evidence = {
            "processing_version": Config.PROCESSING_VERSION,
            "is_reel": d.is_reel,
            "classification": d.classification.value,
            "passage_group": group_key,
            "group_reel_count": group_count,
            "video": None if not video else {"id": video.id, "time": video.event_time.isoformat(), "uuid": video.client_uuid, "reel_count": video.reel_count},
            "skud": None if not skud else {"id": skud.id, "time": skud.event_time.isoformat(), **dict(skud.payload)},
            "raw_ids": [s.raw_id_min, s.raw_id_max],
            "warnings": warnings,
        }
        need_recheck = int(
            d.object_type == ObjectType.UNKNOWN_RFID
            or (d.is_reel and (not video or not skud or final_direction == Direction.UNKNOWN))
        )
        next_recheck = datetime.now() + timedelta(seconds=Config.RECHECK_SEC) if need_recheck else None
        completed = datetime.now()
        first_zone = "OUTER" if stats["first_antenna"] in Config.OUTER_ANTENNAS else "INNER" if stats["first_antenna"] in Config.INNER_ANTENNAS else "UNKNOWN"
        last_zone = "OUTER" if stats["last_antenna"] in Config.OUTER_ANTENNAS else "INNER" if stats["last_antenna"] in Config.INNER_ANTENNAS else "UNKNOWN"
        transport = video.transport if video else "UNKNOWN"
        video_delta = int((video.event_time - s.midpoint).total_seconds() * 1000) if video else None
        skud_delta = int((skud.event_time - s.midpoint).total_seconds() * 1000) if skud else None
        params = {
            "EventKey": s.event_key,
            "SourceTag": s.full_tag,
            "EPC": s.epc,
            "TID": s.tid,
            "Task1CId": task.row_id if task else None,
            "Task1CDt": task.dt if task else None,
            "Task1CDocIds": task.doc_ids if task else None,
            "TaskMatchType": task_match_type,
            "WarehouseId": warehouse.row_id if warehouse else None,
            "WarehouseDt": warehouse.dt if warehouse else None,
            "WarehouseDocIds": warehouse.doc_ids if warehouse else None,
            "FirstSeen": s.first_seen,
            "LastSeen": s.last_seen,
            "CompletedAt": completed,
            "SessionCloseReason": s.close_reason or "TIMEOUT",
            "RfidReadCount": stats["read_count"],
            "DistinctAntennaCount": len(stats["antenna_counts"]),
            "DistinctZoneCount": len([v for k, v in stats["zone_counts"].items() if k != "UNKNOWN" and v]),
            "RfidFirstAntenna": stats["first_antenna"],
            "RfidLastAntenna": stats["last_antenna"],
            "FirstZone": first_zone,
            "LastZone": last_zone,
            "RfidAntennasCsv": ",".join(map(str, sorted(stats["antenna_counts"]))),
            "RfidZonesCsv": ",".join(k for k, v in stats["zone_counts"].items() if v),
            "AvgRSSI": stats["avg_rssi"],
            "MinRSSI": stats["min_rssi"],
            "MaxRSSI": stats["max_rssi"],
            "DurationMs": int(s.duration_sec * 1000),
            "RfidDirection": context.rfid_direction.value,
            "RfidDirectionScore": 4 if context.rfid_direction != Direction.UNKNOWN else 0,
            "VideoMatched": int(video is not None),
            "VideoEventId": video.id if video else None,
            "VideoTime": video.event_time if video else None,
            "VideoDirection": video.direction.value if video else None,
            "VideoTransport": video.transport if video else None,
            "VideoTimeDeltaMs": video_delta,
            "VideoScore": 4 if video else 0,
            "VideoClientEventUuid": video.client_uuid if video else None,
            "SkudMatched": int(skud is not None),
            "SkudExternalId": skud.id if skud else None,
            "SkudTime": skud.event_time if skud else None,
            "SkudDirection": skud.direction.value if skud else None,
            "SkudGate": skud.payload.get("gate") if skud else None,
            "SkudPerson": skud.payload.get("person") if skud else None,
            "SkudCard": skud.payload.get("card") if skud else None,
            "SkudTimeDeltaMs": skud_delta,
            "SkudScore": 3 if skud else 0,
            "FinalDirection": final_direction.value,
            "ConfidencePct": confidence,
            "ConsensusCode": consensus,
            "ScoreIn": score_in,
            "ScoreOut": score_out,
            "SourceCount": len(known),
            "TransportMode": transport,
            "WarningFlags": " | ".join(sorted(set(warnings)))[:1000],
            "EvidenceJson": json.dumps(evidence, ensure_ascii=False, default=str),
            "NeedRecheck": need_recheck,
            "NextRecheckAt": next_recheck,
            "FinalizedAt": None if need_recheck else completed,
            "IsReel": int(d.is_reel),
            "ObjectType": d.object_type.value,
            "ReelClassification": d.classification.value,
            "PassageGroupKey": group_key,
            "GroupReelCount": group_count,
            "RfidMinRawId": s.raw_id_min,
            "RfidMaxRawId": s.raw_id_max,
            "SourceTimeQuality": time_quality,
            "ProcessingVersion": Config.PROCESSING_VERSION,
        }
        columns = list(params)
        cur = conn.cursor()
        cur.execute(f"SELECT TOP(1) 1 FROM {Config.EVENT_TABLE} WITH (UPDLOCK,HOLDLOCK) WHERE EventKey=?", params["EventKey"])
        exists = cur.fetchone() is not None
        if exists:
            update_columns = [c for c in columns if c not in {"EventKey", "CompletedAt"}]
            update_set = ",\n    ".join(f"{c}=?" for c in update_columns)
            cur.execute(
                f"UPDATE {Config.EVENT_TABLE} SET {update_set}, CompletedAt=COALESCE(CompletedAt,?), UpdatedAt=SYSDATETIME() WHERE EventKey=?",
                [params[c] for c in update_columns] + [params["CompletedAt"], params["EventKey"]],
            )
        else:
            insert_cols = ",".join(columns) + ",CreatedAt,UpdatedAt"
            placeholders = ",".join("?" for _ in columns) + ",SYSDATETIME(),SYSDATETIME()"
            cur.execute(
                f"INSERT INTO {Config.EVENT_TABLE}({insert_cols}) VALUES({placeholders})",
                [params[c] for c in columns],
            )

    def checkpoint(self, conn: pyodbc.Connection, last_id: int) -> None:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {Config.ACTIVE_TABLE}")
        for session in self.sessionizer.active.values():
            cur.execute(
                f"INSERT INTO {Config.ACTIVE_TABLE}(SourceTag,SessionJson,MinRawId,MaxRawId,FirstSeen,LastSeen) VALUES(?,?,?,?,?,?)",
                session.full_tag,
                session.to_json(),
                session.raw_id_min,
                session.raw_id_max,
                session.first_seen,
                session.last_seen,
            )
        self.state_set(conn, "LAST_RFID_ID_V3", str(last_id))
        self.state_set(conn, "KPP_V3_HEARTBEAT", json.dumps({"host": socket.gethostname(), "pid": os.getpid(), "at": datetime.now().isoformat()}))

    def close_ready_sessions(self, reads: Sequence[RfidRead], batch_row_count: int, now: datetime) -> List[TagSession]:
        """Закрывает сессии по времени события, не по скорости доставки SQL.

        При backfill/outage `SourceReaderTime` может быть старым, но корректным.
        Пока durable spool продолжает доставку, wall clock не должен дробить
        физическую сессию на множество строк.
        """
        closed: List[TagSession] = []
        if reads:
            self.last_raw_delivery_at = now
            source_watermark = min(max(r.record_time for r in reads), now)
            closed.extend(self.sessionizer.close_stale(source_watermark))
            stream_caught_up = (
                batch_row_count < Config.RFID_BATCH_SIZE
                and (now - source_watermark).total_seconds() <= Config.LIVE_SOURCE_LAG_SEC
            )
            if stream_caught_up:
                closed.extend(self.sessionizer.close_stale(now))
            return closed

        if not self.sessionizer.active:
            return closed
        latest_source = max(s.last_seen for s in self.sessionizer.active.values())
        if (now - latest_source).total_seconds() <= Config.LIVE_SOURCE_LAG_SEC:
            closed.extend(self.sessionizer.close_stale(now))
        elif (now - self.last_raw_delivery_at).total_seconds() >= Config.BACKLOG_QUIET_CLOSE_SEC:
            for session in self.sessionizer.active.values():
                session.warning_flags.append("RFID_BACKLOG_QUIET_CLOSE")
            closed.extend(self.sessionizer.drain("BACKLOG_QUIET_CLOSE"))
        return closed

    def maybe_log_status(
        self, conn: pyodbc.Connection, reads: int, closed: int, invalid: int
    ) -> None:
        now = datetime.now()
        if (now - self.last_status_at).total_seconds() < Config.STATUS_SEC:
            return
        self.last_status_at = now
        cur = conn.cursor()
        cur.execute(f"SELECT ISNULL(MAX(Id),0), MAX(COALESCE(SourceReaderTime,RecordTime)) FROM {Config.RFID_TABLE}")
        row = cur.fetchone()
        source_max = int(row[0] or 0)
        source_time = row[1]
        lag_rows = max(0, source_max - self.last_rfid_id)
        source_age = "нет данных" if source_time is None else f"{(now-source_time).total_seconds():.1f}с"
        last_event_age = "нет" if self.last_event_at is None else f"{(now-self.last_event_at).total_seconds():.1f}с"
        log.info(
            "STATUS cursor=%s source_max=%s lag_rows=%s source_time_age=%s batch_reads=%s batch_closed=%s invalid=%s active=%s total_reads=%s total_closed=%s reels=%s unknown_rfid=%s last_session_age=%s",
            self.last_rfid_id, source_max, lag_rows, source_age, reads, closed, invalid,
            len(self.sessionizer.active), self.total_reads, self.total_closed,
            self.total_reels, self.total_unknown, last_event_age,
        )

    def process_once(self) -> int:
        self.load_registries()
        conn = self.connect()
        snapshot = self.snapshot_active()
        try:
            reads, batch_max_id, row_errors = self.fetch_reads(conn)
            now = datetime.now()
            closed: List[TagSession] = []
            for read in reads:
                closed.extend(self.sessionizer.process(read))
            closed.extend(self.close_ready_sessions(reads, len(reads) + len(row_errors), now))
            self.persist_sessions(conn, closed)
            self.record_processing_errors(conn, row_errors)
            self.checkpoint(conn, batch_max_id)
            conn.commit()
            self.last_rfid_id = batch_max_id
            self.total_reads += len(reads)
            self.total_closed += len(closed)
            if reads or closed or row_errors:
                log.info(
                    "ПАКЕТ COMMIT reads=%s invalid=%s closed=%s active=%s cursor=%s",
                    len(reads), len(row_errors), len(closed), len(self.sessionizer.active), self.last_rfid_id,
                )
            self.maybe_log_status(conn, len(reads), len(closed), len(row_errors))
            return len(reads) + len(row_errors)
        except Exception:
            conn.rollback()
            # Транзакция откатилась — откатываем и память, иначе закрытая в RAM
            # сессия исчезнет и повторный пакет уже не сможет её восстановить.
            self.restore_active_snapshot(snapshot)
            log.exception("Пакет откатан; cursor и in-memory sessions восстановлены")
            raise
        finally:
            conn.close()

    def recheck_pending(self) -> int:
        """Повторно сопоставляет поздние 1С, video и RusGuard.

        Источники асинхронны: событие может быть создано до доставки внешних
        данных. Поэтому NeedRecheck — реальная очередь, а не только флаг UI.
        """
        now = datetime.now()
        if (now - self.last_recheck).total_seconds() < Config.RECHECK_SEC:
            return 0
        self.last_recheck = now
        self.load_registries(force=True)
        cutoff = now - timedelta(hours=max(Config.UNKNOWN_RECHECK_HOURS, Config.EXTERNAL_RECHECK_HOURS))
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
SELECT TOP(1000) EventKey,SourceTag,RfidMinRawId,RfidMaxRawId,SessionCloseReason
FROM {Config.EVENT_TABLE}
WHERE NeedRecheck=1 AND FirstSeen>=?
  AND RfidMinRawId IS NOT NULL AND RfidMaxRawId IS NOT NULL
ORDER BY FirstSeen,EventId;
""",
                cutoff,
            )
            ranges = [
                (str(r[0]), str(r[1] or "").strip().upper(), int(r[2]), int(r[3]), str(r[4] or "RECHECK"))
                for r in cur.fetchall()
            ]
            sessions: List[TagSession] = []
            for _key, source_tag, min_id, max_id, close_reason in ranges:
                cur.execute(
                    f"SELECT Id,COALESCE(SourceReaderTime,RecordTime),Antenna,RSSI,EPC,TID,ReceivedAt,COALESCE(TimeQuality,'LEGACY_RECORD_TIME'),CONVERT(varchar(36),IngestBatchId) FROM {Config.RFID_TABLE} WHERE Id BETWEEN ? AND ? ORDER BY Id",
                    min_id,
                    max_id,
                )
                session = self.build_recheck_session(source_tag, cur.fetchall(), close_reason)
                if session is not None:
                    sessions.append(session)
            self.persist_sessions(conn, sessions)

            # 1С/склад больше не считаются ожидаемыми после строгого окна ±24 ч.
            cur.execute(
                f"""
UPDATE {Config.EVENT_TABLE}
SET NeedRecheck=0, FinalizedAt=COALESCE(FinalizedAt,SYSDATETIME()), UpdatedAt=SYSDATETIME(),
    WarningFlags=CONCAT(ISNULL(WarningFlags,''), CASE WHEN ISNULL(WarningFlags,'')='' THEN '' ELSE ' | ' END, 'REEL_NOT_CONFIRMED_WITHIN_24H')
WHERE IsReel=0 AND ObjectType='UNKNOWN_RFID' AND NeedRecheck=1
  AND FirstSeen<DATEADD(hour,-?,SYSDATETIME());
""",
                Config.TASK_WINDOW_HOURS,
            )
            # Видео/СКУД могут опоздать, но очередь не должна быть вечной.
            cur.execute(
                f"""
UPDATE {Config.EVENT_TABLE}
SET NeedRecheck=0, FinalizedAt=COALESCE(FinalizedAt,SYSDATETIME()), UpdatedAt=SYSDATETIME(),
    WarningFlags=CONCAT(ISNULL(WarningFlags,''), CASE WHEN ISNULL(WarningFlags,'')='' THEN '' ELSE ' | ' END, 'EXTERNAL_MATCH_RECHECK_EXPIRED')
WHERE IsReel=1 AND NeedRecheck=1
  AND FirstSeen<DATEADD(hour,-?,SYSDATETIME());
""",
                Config.EXTERNAL_RECHECK_HOURS,
            )
            conn.commit()
        if sessions:
            log.info("Повторно проверено незавершённых RFID-событий: %s", len(sessions))
        return len(sessions)

    def run(self, once: bool = False) -> None:
        self.bootstrap()
        while True:
            try:
                count = self.process_once()
                self.recheck_pending()
                if once:
                    return
                if count >= Config.RFID_BATCH_SIZE:
                    continue
                time.sleep(Config.POLL_SEC)
            except KeyboardInterrupt:
                log.warning("Остановка пользователем. Активные сессии уже durable, искусственно не закрываются.")
                return
            except Exception:
                if once:
                    raise
                time.sleep(max(2.0, Config.POLL_SEC))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="обработать один пакет и выйти")
    args = parser.parse_args()
    Aggregator().run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
