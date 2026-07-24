#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чистая бизнес-логика RFID КПП v3 без зависимости от БД.

Модуль специально отделён от pyodbc/OpenCV, чтобы ключевые правила можно было
регрессионно тестировать: разбиение RFID-сессий, классификацию катушки,
группировку проходов и взаимно-однозначное распределение видео/СКУД.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


class Direction(str, Enum):
    IN = "IN"
    OUT = "OUT"
    UNKNOWN = "UNKNOWN"


class ObjectType(str, Enum):
    REEL = "REEL"
    UNKNOWN_RFID = "UNKNOWN_RFID"
    OTHER_RFID = "OTHER_RFID"


class ReelClassification(str, Enum):
    FULL_TAG_1C = "FULL_TAG_1C"
    FULL_TAG_WAREHOUSE = "FULL_TAG_WAREHOUSE"
    FULL_TAG_BOTH = "FULL_TAG_BOTH"
    EPC_UNIQUE_1C = "EPC_UNIQUE_1C"
    EPC_UNIQUE_WAREHOUSE = "EPC_UNIQUE_WAREHOUSE"
    NOT_CONFIRMED = "NOT_CONFIRMED"
    EXPLICIT_NON_REEL = "EXPLICIT_NON_REEL"


@dataclass(frozen=True)
class RfidRead:
    id: int
    record_time: datetime
    antenna: int
    rssi: float
    epc: str
    tid: str = ""
    received_at: Optional[datetime] = None
    time_quality: str = "SOURCE_TIME"
    ingest_batch_id: Optional[str] = None

    @property
    def full_tag(self) -> str:
        return f"{self.epc or ''}{self.tid or ''}".strip().upper()


@dataclass
class TagSession:
    full_tag: str
    epc: str
    tid: str
    first_seen: datetime
    last_seen: datetime
    reads: List[RfidRead] = field(default_factory=list)
    close_reason: str = ""
    warning_flags: List[str] = field(default_factory=list)

    def add(self, read: RfidRead) -> bool:
        """Добавляет чтение идемпотентно. Возвращает False для уже известного Id."""
        if any(existing.id == read.id for existing in self.reads):
            return False
        self.reads.append(read)
        self.reads.sort(key=lambda r: (r.record_time, r.id))
        self.first_seen = self.reads[0].record_time
        self.last_seen = self.reads[-1].record_time
        return True

    @property
    def raw_id_min(self) -> int:
        return min((r.id for r in self.reads), default=0)

    @property
    def raw_id_max(self) -> int:
        return max((r.id for r in self.reads), default=0)

    @property
    def duration_sec(self) -> float:
        return max(0.0, (self.last_seen - self.first_seen).total_seconds())

    @property
    def midpoint(self) -> datetime:
        return self.first_seen + (self.last_seen - self.first_seen) / 2

    @property
    def event_key(self) -> str:
        payload = (
            f"{self.full_tag}|{self.first_seen.isoformat(timespec='milliseconds')}|"
            f"{self.last_seen.isoformat(timespec='milliseconds')}|{self.raw_id_min}|{self.raw_id_max}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def to_json(self) -> str:
        payload = {
            "full_tag": self.full_tag,
            "epc": self.epc,
            "tid": self.tid,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "close_reason": self.close_reason,
            "warning_flags": self.warning_flags,
            "reads": [
                {
                    "id": r.id,
                    "record_time": r.record_time.isoformat(),
                    "antenna": r.antenna,
                    "rssi": r.rssi,
                    "epc": r.epc,
                    "tid": r.tid,
                    "received_at": r.received_at.isoformat() if r.received_at else None,
                    "time_quality": r.time_quality,
                    "ingest_batch_id": r.ingest_batch_id,
                }
                for r in self.reads
            ],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "TagSession":
        obj = json.loads(raw)
        session = cls(
            full_tag=obj["full_tag"],
            epc=obj["epc"],
            tid=obj.get("tid", ""),
            first_seen=datetime.fromisoformat(obj["first_seen"]),
            last_seen=datetime.fromisoformat(obj["last_seen"]),
            close_reason=obj.get("close_reason", ""),
            warning_flags=list(obj.get("warning_flags", [])),
        )
        for item in obj.get("reads", []):
            session.add(
                RfidRead(
                    id=int(item["id"]),
                    record_time=datetime.fromisoformat(item["record_time"]),
                    antenna=int(item["antenna"]),
                    rssi=float(item.get("rssi", 0.0)),
                    epc=str(item.get("epc", "")),
                    tid=str(item.get("tid", "")),
                    received_at=datetime.fromisoformat(item["received_at"]) if item.get("received_at") else None,
                    time_quality=str(item.get("time_quality", "SOURCE_TIME")),
                    ingest_batch_id=str(item["ingest_batch_id"]) if item.get("ingest_batch_id") else None,
                )
            )
        return session


@dataclass(frozen=True)
class RegistryRecord:
    row_id: int
    dt: datetime
    full_tag: str
    doc_ids: str = ""
    series_number: str = ""

    @property
    def epc(self) -> str:
        return self.full_tag[:24].upper() if self.full_tag else ""


@dataclass(frozen=True)
class ReelDecision:
    is_reel: bool
    object_type: ObjectType
    classification: ReelClassification
    task: Optional[RegistryRecord] = None
    warehouse: Optional[RegistryRecord] = None
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TimedExternalEvent:
    id: int
    event_time: datetime
    direction: Direction = Direction.UNKNOWN
    transport: str = "UNKNOWN"
    reel_count: Optional[int] = None
    client_uuid: Optional[str] = None
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass
class PassageGroup:
    sessions: List[TagSession]
    direction: Direction
    anchor_time: datetime
    group_key: str
    video: Optional[TimedExternalEvent] = None
    skud: Optional[TimedExternalEvent] = None

    @property
    def reel_count(self) -> int:
        return len(self.sessions)


class StrictSessionizer:
    """Потоковый сессионализатор, который закрывает сессию ДО добавления чтения.

    Это устраняет основной дефект v2.4: накопившийся пакет больше не может
    растянуть одну сессию на часы/сутки. Поздние чтения не выбрасываются — они
    образуют отдельную диагностическую сессию.
    """

    def __init__(
        self,
        gap_sec: float = 35.0,
        max_duration_sec: float = 900.0,
        late_tolerance_sec: float = 2.0,
    ) -> None:
        self.gap = timedelta(seconds=gap_sec)
        self.max_duration = timedelta(seconds=max_duration_sec)
        self.late_tolerance = timedelta(seconds=late_tolerance_sec)
        self.active: Dict[str, TagSession] = {}

    @staticmethod
    def _new(read: RfidRead) -> TagSession:
        session = TagSession(
            full_tag=read.full_tag,
            epc=read.epc.upper(),
            tid=(read.tid or "").upper(),
            first_seen=read.record_time,
            last_seen=read.record_time,
        )
        session.add(read)
        return session

    def restore(self, sessions: Iterable[TagSession]) -> None:
        for session in sessions:
            self.active[session.full_tag] = session

    def process(self, read: RfidRead) -> List[TagSession]:
        if not read.full_tag:
            return []
        current = self.active.get(read.full_tag)
        if current is None:
            self.active[read.full_tag] = self._new(read)
            return []

        # Идемпотентный replay после сбоя/checkpoint.
        if any(r.id == read.id for r in current.reads):
            return []

        # Смена соединения сама по себе не доказывает новый физический проход.
        # Контроллер может переподключиться прямо во время движения катушки, поэтому
        # разделение выполняется только обычными gap/max-duration правилами. Эпоха
        # сохраняется как диагностический признак качества времени.
        current_batch = next((r.ingest_batch_id for r in reversed(current.reads) if r.ingest_batch_id), None)
        if current_batch and read.ingest_batch_id and current_batch != read.ingest_batch_id:
            if "RFID_READER_CONNECTION_EPOCH_CHANGED_WITHIN_SESSION" not in current.warning_flags:
                current.warning_flags.append("RFID_READER_CONNECTION_EPOCH_CHANGED_WITHIN_SESSION")

        # Действительно поздняя строка не должна расширять текущую сессию назад.
        if read.record_time < current.first_seen - self.late_tolerance:
            late = self._new(read)
            late.close_reason = "LATE_DATA"
            late.warning_flags.append("RFID_READER_TIME_OUT_OF_ORDER")
            return [late]

        gap = read.record_time - current.last_seen
        duration = read.record_time - current.first_seen
        if gap > self.gap or duration > self.max_duration:
            current.close_reason = "GAP" if gap > self.gap else "MAX_DURATION"
            self.active[read.full_tag] = self._new(read)
            return [current]

        current.add(read)
        return []

    def close_stale(self, now: datetime) -> List[TagSession]:
        closed: List[TagSession] = []
        for tag, session in list(self.active.items()):
            idle = now - session.last_seen
            if idle > self.gap:
                session.close_reason = "TIMEOUT"
                closed.append(session)
                del self.active[tag]
            elif session.last_seen - session.first_seen >= self.max_duration:
                session.close_reason = "MAX_DURATION"
                closed.append(session)
                del self.active[tag]
        return closed

    def drain(self, reason: str = "SHUTDOWN") -> List[TagSession]:
        result = list(self.active.values())
        for session in result:
            session.close_reason = reason
        self.active.clear()
        return result


def infer_rfid_direction(session: TagSession, outer: Set[int], inner: Set[int]) -> Direction:
    if not session.reads:
        return Direction.UNKNOWN
    zones: List[str] = []
    for read in sorted(session.reads, key=lambda r: (r.record_time, r.id)):
        zone = "OUTER" if read.antenna in outer else "INNER" if read.antenna in inner else "UNKNOWN"
        if zone != "UNKNOWN" and (not zones or zones[-1] != zone):
            zones.append(zone)
    if len(zones) < 2:
        return Direction.UNKNOWN
    if zones[0] == "OUTER" and zones[-1] == "INNER":
        return Direction.IN
    if zones[0] == "INNER" and zones[-1] == "OUTER":
        return Direction.OUT
    return Direction.UNKNOWN


def _within(records: Sequence[RegistryRecord], event_time: datetime, hours: float) -> List[RegistryRecord]:
    limit = abs(hours) * 3600.0
    return [r for r in records if abs((r.dt - event_time).total_seconds()) <= limit]


def _nearest(records: Sequence[RegistryRecord], event_time: datetime) -> Optional[RegistryRecord]:
    if not records:
        return None
    return min(records, key=lambda r: (abs((r.dt - event_time).total_seconds()), -r.row_id))


def classify_reel(
    session: TagSession,
    tasks_by_full: Mapping[str, Sequence[RegistryRecord]],
    warehouse_by_full: Mapping[str, Sequence[RegistryRecord]],
    tasks_by_epc: Mapping[str, Sequence[RegistryRecord]],
    warehouse_by_epc: Mapping[str, Sequence[RegistryRecord]],
    window_hours: float = 24.0,
    allow_unique_epc: bool = False,
    explicit_non_reel_tags: Optional[Set[str]] = None,
) -> ReelDecision:
    """Классифицирует RFID-объект как катушку только по авторитетным данным.

    Неизвестная RFID-метка не считается катушкой. EPC-only по умолчанию запрещён,
    потому что один EPC может принадлежать тысячам разных TID.
    """
    tag = session.full_tag.upper()
    explicit_non_reel_tags = explicit_non_reel_tags or set()
    if tag in explicit_non_reel_tags:
        return ReelDecision(False, ObjectType.OTHER_RFID, ReelClassification.EXPLICIT_NON_REEL)

    event_time = session.midpoint
    tasks = _within(list(tasks_by_full.get(tag, ())), event_time, window_hours)
    warehouses = _within(list(warehouse_by_full.get(tag, ())), event_time, window_hours)
    task = _nearest(tasks, event_time)
    warehouse = _nearest(warehouses, event_time)
    if task and warehouse:
        return ReelDecision(True, ObjectType.REEL, ReelClassification.FULL_TAG_BOTH, task, warehouse)
    if task:
        return ReelDecision(True, ObjectType.REEL, ReelClassification.FULL_TAG_1C, task, None)
    if warehouse:
        return ReelDecision(True, ObjectType.REEL, ReelClassification.FULL_TAG_WAREHOUSE, None, warehouse)

    if allow_unique_epc and session.epc:
        task_epc = _within(list(tasks_by_epc.get(session.epc.upper(), ())), event_time, window_hours)
        wh_epc = _within(list(warehouse_by_epc.get(session.epc.upper(), ())), event_time, window_hours)
        # Уникальность проверяется по полной метке, не по числу строк документа.
        task_tags = {r.full_tag.upper() for r in task_epc}
        wh_tags = {r.full_tag.upper() for r in wh_epc}
        if len(task_tags) == 1:
            task = _nearest(task_epc, event_time)
            return ReelDecision(
                True,
                ObjectType.REEL,
                ReelClassification.EPC_UNIQUE_1C,
                task=task,
                warnings=("REEL_CONFIRMED_BY_UNIQUE_EPC_WITHOUT_TID",),
            )
        if len(wh_tags) == 1:
            warehouse = _nearest(wh_epc, event_time)
            return ReelDecision(
                True,
                ObjectType.REEL,
                ReelClassification.EPC_UNIQUE_WAREHOUSE,
                warehouse=warehouse,
                warnings=("REEL_CONFIRMED_BY_UNIQUE_EPC_WITHOUT_TID",),
            )

    return ReelDecision(
        False,
        ObjectType.UNKNOWN_RFID,
        ReelClassification.NOT_CONFIRMED,
        warnings=("RFID_OBJECT_NOT_CONFIRMED_AS_REEL",),
    )


def group_reel_sessions(
    sessions: Sequence[TagSession],
    direction_by_event_key: Mapping[str, Direction],
    window_sec: float = 4.0,
) -> List[PassageGroup]:
    """Объединяет одновременно прошедшие катушки в физическую группу прохода."""
    ordered = sorted(sessions, key=lambda s: (s.midpoint, s.event_key))
    groups: List[List[TagSession]] = []
    for session in ordered:
        direction = direction_by_event_key.get(session.event_key, Direction.UNKNOWN)
        if not groups:
            groups.append([session])
            continue
        last_group = groups[-1]
        group_anchor = min(s.midpoint for s in last_group)
        group_dirs = {
            direction_by_event_key.get(s.event_key, Direction.UNKNOWN)
            for s in last_group
            if direction_by_event_key.get(s.event_key, Direction.UNKNOWN) != Direction.UNKNOWN
        }
        compatible = direction == Direction.UNKNOWN or not group_dirs or direction in group_dirs
        if compatible and abs((session.midpoint - group_anchor).total_seconds()) <= window_sec:
            last_group.append(session)
        else:
            groups.append([session])

    result: List[PassageGroup] = []
    for members in groups:
        dirs = [direction_by_event_key.get(s.event_key, Direction.UNKNOWN) for s in members]
        known = [d for d in dirs if d != Direction.UNKNOWN]
        direction = max(set(known), key=known.count) if known else Direction.UNKNOWN
        anchor = sum((s.midpoint.timestamp() for s in members), 0.0) / len(members)
        anchor_dt = datetime.fromtimestamp(anchor, tz=members[0].midpoint.tzinfo)
        payload = "|".join(sorted(s.event_key for s in members))
        key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
        result.append(PassageGroup(members, direction, anchor_dt, key))
    return result


def event_cost(
    group: PassageGroup,
    event: TimedExternalEvent,
    expected_lag_sec: float,
    before_sec: float,
    after_sec: float,
    direction_penalty_sec: float = 20.0,
    count_penalty_sec: float = 8.0,
) -> Optional[float]:
    expected = group.anchor_time + timedelta(seconds=expected_lag_sec)
    signed_delta = (event.event_time - expected).total_seconds()
    if signed_delta < -before_sec or signed_delta > after_sec:
        return None
    cost = abs(signed_delta)
    if group.direction != Direction.UNKNOWN and event.direction != Direction.UNKNOWN and group.direction != event.direction:
        cost += direction_penalty_sec
    if event.reel_count is not None:
        count_delta = abs(event.reel_count - group.reel_count)
        # Сильно несовместимое число катушек означает другое физическое событие.
        # Небольшая ошибка YOLO (±1) допускается и учитывается штрафом.
        if count_delta > max(1, math.ceil(group.reel_count * 0.5)):
            return None
        cost += count_delta * count_penalty_sec
    return cost


def minimum_cost_bipartite_pairs(
    left_count: int,
    right_count: int,
    candidates: Sequence[Tuple[float, int, int]],
) -> List[Tuple[int, int]]:
    """Максимальное по числу и минимальное по стоимости двудольное сопоставление.

    Реализовано как min-cost max-flow без внешних зависимостей. В отличие от
    жадного выбора не теряет допустимую пару в плотном потоке, когда лучший
    локальный выбор блокирует единственный вариант соседнего объекта.
    """
    if left_count <= 0 or right_count <= 0 or not candidates:
        return []

    source = 0
    left_base = 1
    right_base = left_base + left_count
    sink = right_base + right_count
    node_count = sink + 1

    class Edge:
        __slots__ = ("to", "rev", "cap", "cost", "left", "right")

        def __init__(self, to: int, rev: int, cap: int, cost: int, left: int = -1, right: int = -1) -> None:
            self.to = to
            self.rev = rev
            self.cap = cap
            self.cost = cost
            self.left = left
            self.right = right

    graph: List[List[Edge]] = [[] for _ in range(node_count)]

    def add_edge(u: int, v: int, cap: int, cost: int, left: int = -1, right: int = -1) -> None:
        fwd = Edge(v, len(graph[v]), cap, cost, left, right)
        rev = Edge(u, len(graph[u]), 0, -cost)
        graph[u].append(fwd)
        graph[v].append(rev)

    for li in range(left_count):
        add_edge(source, left_base + li, 1, 0)
    for ri in range(right_count):
        add_edge(right_base + ri, sink, 1, 0)

    tie_span = max(1, left_count * right_count + 1)
    seen_edges: Set[Tuple[int, int]] = set()
    for raw_cost, li, ri in candidates:
        if not (0 <= li < left_count and 0 <= ri < right_count):
            continue
        if (li, ri) in seen_edges or not math.isfinite(raw_cost):
            continue
        seen_edges.add((li, ri))
        # 1 мс точности достаточно для временных окон; младшая часть только
        # детерминирует равные стоимости и не меняет основной порядок.
        base_cost = max(0, int(round(raw_cost * 1000.0)))
        stable_cost = base_cost * tie_span + li * right_count + ri
        add_edge(left_base + li, right_base + ri, 1, stable_cost, li, ri)

    inf = 10**30
    while True:
        dist = [inf] * node_count
        prev_node = [-1] * node_count
        prev_edge = [-1] * node_count
        dist[source] = 0
        # Bellman-Ford корректен при отрицательных residual-ребрах и для
        # ожидаемого малого числа событий в одном временном окне достаточно быстр.
        for _ in range(node_count - 1):
            changed = False
            for u in range(node_count):
                if dist[u] == inf:
                    continue
                for ei, edge in enumerate(graph[u]):
                    if edge.cap <= 0:
                        continue
                    nd = dist[u] + edge.cost
                    if nd < dist[edge.to]:
                        dist[edge.to] = nd
                        prev_node[edge.to] = u
                        prev_edge[edge.to] = ei
                        changed = True
            if not changed:
                break
        if dist[sink] == inf:
            break
        v = sink
        while v != source:
            u = prev_node[v]
            ei = prev_edge[v]
            if u < 0 or ei < 0:
                raise RuntimeError("Некорректный residual path")
            edge = graph[u][ei]
            edge.cap -= 1
            graph[v][edge.rev].cap += 1
            v = u

    result: List[Tuple[int, int]] = []
    for li in range(left_count):
        node = left_base + li
        for edge in graph[node]:
            if edge.left == li and edge.right >= 0 and edge.cap == 0:
                result.append((li, edge.right))
    result.sort()
    return result


def assign_events_one_to_one(
    groups: Sequence[PassageGroup],
    events: Sequence[TimedExternalEvent],
    expected_lag_by_direction: Mapping[Direction, float],
    before_sec: float,
    after_sec: float,
    reserved_event_ids: Optional[Set[int]] = None,
) -> Dict[str, TimedExternalEvent]:
    """Глобально распределяет события: одно внешнее событие — одной группе.

    Сначала максимизируется число допустимых связей, затем минимизируется их
    суммарная стоимость. One-to-many допускается только внутри PassageGroup,
    где явно известно количество катушек.
    """
    reserved = set(reserved_event_ids or set())
    available_events = [event for event in events if event.id not in reserved]
    candidates: List[Tuple[float, int, int]] = []
    for gi, group in enumerate(groups):
        lag = expected_lag_by_direction.get(group.direction, expected_lag_by_direction.get(Direction.UNKNOWN, 0.0))
        for ei, event in enumerate(available_events):
            cost = event_cost(group, event, lag, before_sec, after_sec)
            if cost is not None:
                candidates.append((cost, gi, ei))

    result: Dict[str, TimedExternalEvent] = {}
    for gi, ei in minimum_cost_bipartite_pairs(len(groups), len(available_events), candidates):
        result[groups[gi].group_key] = available_events[ei]
    return result


def normalize_video_direction(raw: object, camera_in_direction: str = "0>1") -> Direction:
    text = str(raw or "").strip().upper()
    if text == camera_in_direction.upper():
        return Direction.IN
    reverse = ">".join(reversed(camera_in_direction.split(">")))
    if text == reverse.upper():
        return Direction.OUT
    if text in {"IN", "ВХОД", "ВЪЕЗД"}:
        return Direction.IN
    if text in {"OUT", "ВЫХОД", "ВЫЕЗД"}:
        return Direction.OUT
    return Direction.UNKNOWN


def session_statistics(session: TagSession, outer: Set[int], inner: Set[int]) -> Dict[str, object]:
    antenna_counts: Dict[int, int] = {}
    zone_counts: Dict[str, int] = {}
    for read in session.reads:
        antenna_counts[read.antenna] = antenna_counts.get(read.antenna, 0) + 1
        zone = "OUTER" if read.antenna in outer else "INNER" if read.antenna in inner else "UNKNOWN"
        zone_counts[zone] = zone_counts.get(zone, 0) + 1
    rssis = [r.rssi for r in session.reads]
    ordered = sorted(session.reads, key=lambda r: (r.record_time, r.id))
    return {
        "read_count": len(ordered),
        "antenna_counts": antenna_counts,
        "zone_counts": zone_counts,
        "first_antenna": ordered[0].antenna if ordered else None,
        "last_antenna": ordered[-1].antenna if ordered else None,
        "avg_rssi": round(sum(rssis) / len(rssis), 2) if rssis else None,
        "min_rssi": round(min(rssis), 2) if rssis else None,
        "max_rssi": round(max(rssis), 2) if rssis else None,
    }
