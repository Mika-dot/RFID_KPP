#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RTSP + YOLO + durable DB queue v3.

Исправлено:
- timestamp берётся в момент захвата кадра, а не после YOLO;
- старый/замороженный кадр повторно не обрабатывается;
- у каждой катушки собственный track ID, глобального cooldown нет;
- cross-camera matching взаимно-однозначный;
- событие считается принятым только после durable enqueue в SQLite;
- DB writer работает отдельно, с retry и идемпотентным ClientEventUuid;
- JPEG хранится в VARBINARY(MAX) ImageData;
- несколько почти одновременных катушек объединяются в один видеопереход с ReelCount.

Перед запуском выполнить migrations/001_kpp_v3_reliability.sql.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))
from common.kpp_core_v3 import minimum_cost_bipartite_pairs  # noqa: E402
from common.single_instance import SingleInstanceLock  # noqa: E402


logging.basicConfig(
    level=getattr(logging, os.getenv("RFID_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rtsp-v3")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


class Config:
    DB_CONN = os.getenv("RFID_DB_CONNECTION", "")
    DB_TABLE = os.getenv("RFID_DB_LOG_TABLE", "dbo.ReelTransitions")
    MODEL_PATH = os.getenv("RFID_MODEL_PATH", "runs/detect/rfid_forklift_reel2/weights/best.pt")
    REEL_CLASS = os.getenv("RFID_REEL_CLASS_NAME", "cable_reel")
    CONF = float(os.getenv("RFID_CONFIDENCE_THRESHOLD", "0.25"))
    IOU = float(os.getenv("RFID_IOU_THRESHOLD", "0.45"))
    DEVICE = os.getenv("RFID_YOLO_DEVICE", "cpu")

    CAMERA_IDS = [int(x) for x in os.getenv("RFID_CAMERA_IDS", "0,1").split(",")]
    RTSP_URLS = [os.getenv(f"RFID_RTSP_{cid}", "") for cid in CAMERA_IDS]
    MASKS = {cid: os.getenv(f"RFID_MASK_{cid}", "") for cid in CAMERA_IDS}
    MASK_ENABLED = env_bool("RFID_MASK_ENABLED", True)
    MAX_FRAME_AGE_SEC = float(os.getenv("RFID_MAX_FRAME_AGE_SEC", "2.0"))
    RECONNECT_SEC = float(os.getenv("RFID_RTSP_RECONNECT_SEC", "3"))
    RTSP_BACKEND = os.getenv("RFID_RTSP_BACKEND", "FFMPEG").strip().upper()
    SET_CAPTURE_BUFFER = env_bool("RFID_SET_CAPTURE_BUFFER", False)

    TRACK_MAX_DISTANCE_PX = float(os.getenv("RFID_TRACK_MAX_DISTANCE_PX", "140"))
    TRACK_MAX_AGE_SEC = float(os.getenv("RFID_TRACK_MAX_AGE_SEC", "4"))
    TRACK_MIN_HITS = int(os.getenv("RFID_TRACK_MIN_HITS", "2"))
    TRANSITION_MIN_SEC = float(os.getenv("RFID_TRANSITION_MIN_SEC", "0.05"))
    TRANSITION_MAX_SEC = float(os.getenv("RFID_TRANSITION_WINDOW_SEC", "30"))
    CROSS_AREA_LOG_WEIGHT = float(os.getenv("RFID_CROSS_AREA_LOG_WEIGHT", "0.7"))
    CROSS_POSITION_WEIGHT = float(os.getenv("RFID_CROSS_POSITION_WEIGHT", "0.15"))
    CROSS_MAX_COST = float(os.getenv("RFID_CROSS_MAX_COST", "35"))
    CONTEXT_DISTANCE_PX = float(os.getenv("RFID_REEL_NEARBY_THRESHOLD_PX", "300"))

    GROUP_WINDOW_SEC = float(os.getenv("RFID_VIDEO_GROUP_WINDOW_SEC", "1.5"))
    SPOOL_PATH = os.getenv("RFID_VIDEO_SPOOL", "recordings/video_spool_v3.sqlite")
    RETRY_BASE_SEC = float(os.getenv("RFID_DB_RETRY_BASE_SEC", "2"))
    RETRY_MAX_SEC = float(os.getenv("RFID_DB_RETRY_MAX_SEC", "300"))
    IMAGE_QUALITY = int(os.getenv("RFID_IMAGE_QUALITY", "75"))
    IMAGE_MAX_WIDTH = int(os.getenv("RFID_IMAGE_MAX_WIDTH", "640"))
    IMAGE_MAX_HEIGHT = int(os.getenv("RFID_IMAGE_MAX_HEIGHT", "480"))
    CSV_PATH = os.getenv("RFID_REEL_TRANSITION_LOG_PATH", "recordings/reel_transitions_v3.csv")
    HEADLESS = env_bool("RFID_HEADLESS", False)
    LOCK_PATH = os.getenv("RFID_VIDEO_LOCK_FILE", SPOOL_PATH + ".lock")
    SPOOL_RETENTION_DAYS = int(os.getenv("RFID_VIDEO_SPOOL_RETENTION_DAYS", "14"))
    SPOOL_WARN_MB = int(os.getenv("RFID_VIDEO_SPOOL_WARN_MB", "4096"))
    WINDOW_WIDTH = int(os.getenv("RFID_WINDOW_WIDTH", "640"))
    WINDOW_HEIGHT = int(os.getenv("RFID_WINDOW_HEIGHT", "480"))
    HEARTBEAT_SEC = float(os.getenv("RFID_VIDEO_HEARTBEAT_SEC", "10"))


@dataclass
class FramePacket:
    frame: np.ndarray
    captured_at: datetime
    captured_mono: float
    sequence: int


@dataclass(frozen=True)
class Detection:
    class_name: str
    confidence: float
    bbox: Tuple[int, int, int, int]

    @property
    def center(self) -> Tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return max(1.0, float(max(0, x2 - x1) * max(0, y2 - y1)))


@dataclass
class Track:
    track_id: str
    camera_id: int
    first_seen: datetime
    last_seen: datetime
    bbox: Tuple[int, int, int, int]
    hits: int = 1
    context: str = "alone"
    matched: bool = False
    pending: bool = False
    frame_shape: Tuple[int, int] = (1, 1)

    @property
    def center_normalized(self) -> Tuple[float, float]:
        h, w = self.frame_shape
        x1, y1, x2, y2 = self.bbox
        return (((x1 + x2) / 2.0) / max(1, w), ((y1 + y2) / 2.0) / max(1, h))

    @property
    def area_normalized(self) -> float:
        h, w = self.frame_shape
        x1, y1, x2, y2 = self.bbox
        return max(1.0 / max(1, h * w), max(0, x2 - x1) * max(0, y2 - y1) / max(1, h * w))


@dataclass
class RawTransition:
    direction: str
    from_camera: int
    to_camera: int
    started_at: datetime
    captured_at: datetime
    processed_at: datetime
    time_diff_sec: float
    transport: str
    source_track_id: str
    target_track_id: str
    image: Optional[np.ndarray]
    source_track: Track
    target_track: Track


@dataclass
class GroupedTransition:
    event_uuid: str
    direction: str
    from_camera: int
    to_camera: int
    captured_at: datetime
    processed_at: datetime
    time_diff_sec: float
    transport: str
    reel_count: int
    source_track_ids: List[str]
    image_bytes: Optional[bytes]
    track_refs: List[Track] = field(default_factory=list, repr=False)


class RTSPStream:
    def __init__(self, url: str, camera_id: int) -> None:
        self.url = url
        self.camera_id = camera_id
        self.lock = threading.Lock()
        self.latest: Optional[FramePacket] = None
        self.sequence = 0
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.cap = None

    def start(self) -> "RTSPStream":
        if not self.url:
            raise ValueError(f"Не задан RFID_RTSP для camera_id={self.camera_id}")
        self.running = True
        self.thread = threading.Thread(target=self._loop, name=f"rtsp-{self.camera_id}", daemon=True)
        self.thread.start()
        return self

    def _loop(self) -> None:
        while self.running:
            try:
                if self.cap:
                    self.cap.release()
                backend = cv2.CAP_FFMPEG if Config.RTSP_BACKEND == "FFMPEG" else cv2.CAP_ANY
                self.cap = cv2.VideoCapture(self.url, backend)
                if Config.SET_CAPTURE_BUFFER:
                    try:
                        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    except Exception:
                        log.debug("Camera %s does not support CAP_PROP_BUFFERSIZE", self.camera_id)
                if not self.cap.isOpened():
                    raise ConnectionError("поток не открыт")
                log.info("Камера %s подключена", self.camera_id)
                while self.running:
                    ok, frame = self.cap.read()
                    if not ok:
                        raise ConnectionError("кадр не получен")
                    packet = FramePacket(frame.copy(), datetime.now(), time.monotonic(), self.sequence + 1)
                    with self.lock:
                        self.sequence = packet.sequence
                        self.latest = packet
            except Exception as exc:
                log.warning("Камера %s: %s; reconnect", self.camera_id, exc)
                time.sleep(Config.RECONNECT_SEC)

    def read_new(self, after_sequence: int) -> Optional[FramePacket]:
        with self.lock:
            packet = self.latest
            if packet is None or packet.sequence <= after_sequence:
                return None
            if time.monotonic() - packet.captured_mono > Config.MAX_FRAME_AGE_SEC:
                return None
            return FramePacket(packet.frame.copy(), packet.captured_at, packet.captured_mono, packet.sequence)

    def stop(self) -> None:
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)
        if self.cap:
            self.cap.release()


class CentroidTracker:
    def __init__(self, camera_id: int) -> None:
        self.camera_id = camera_id
        self.tracks: Dict[str, Track] = {}
        self.sequence = 0

    @staticmethod
    def distance(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
        ac = ((a[0] + a[2]) / 2.0, (a[1] + a[3]) / 2.0)
        bc = ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)
        return math.hypot(ac[0] - bc[0], ac[1] - bc[1])

    def update(self, detections: Sequence[Detection], captured_at: datetime, frame_shape: Tuple[int, int]) -> List[Track]:
        active = [t for t in self.tracks.values() if (captured_at - t.last_seen).total_seconds() <= Config.TRACK_MAX_AGE_SEC and not t.matched]
        pairs: List[Tuple[float, str, int]] = []
        for track in active:
            for idx, det in enumerate(detections):
                dist = self.distance(track.bbox, det.bbox)
                if dist <= Config.TRACK_MAX_DISTANCE_PX:
                    pairs.append((dist, track.track_id, idx))
        pairs.sort()
        used_tracks: set[str] = set()
        used_dets: set[int] = set()
        for _dist, track_id, idx in pairs:
            if track_id in used_tracks or idx in used_dets:
                continue
            track = self.tracks[track_id]
            track.bbox = detections[idx].bbox
            track.last_seen = captured_at
            track.hits += 1
            track.frame_shape = frame_shape
            used_tracks.add(track_id)
            used_dets.add(idx)
        for idx, det in enumerate(detections):
            if idx in used_dets:
                continue
            self.sequence += 1
            track_id = f"C{self.camera_id}-{self.sequence}"
            self.tracks[track_id] = Track(track_id, self.camera_id, captured_at, captured_at, det.bbox, frame_shape=frame_shape)
        self.cleanup(captured_at)
        return [t for t in self.tracks.values() if t.hits >= Config.TRACK_MIN_HITS and not t.matched and not t.pending]

    def cleanup(self, now: datetime) -> None:
        for track_id, track in list(self.tracks.items()):
            if (now - track.last_seen).total_seconds() > Config.TRANSITION_MAX_SEC * 2:
                del self.tracks[track_id]


class CrossCameraMatcher:
    def __init__(self, camera_ids: Sequence[int]) -> None:
        self.camera_ids = list(camera_ids)
        self.history: Dict[int, Dict[str, Track]] = {cid: {} for cid in camera_ids}

    @staticmethod
    def cost(source: Track, target: Track) -> Optional[float]:
        dt = (target.first_seen - source.last_seen).total_seconds()
        if dt < Config.TRANSITION_MIN_SEC or dt > Config.TRANSITION_MAX_SEC:
            return None
        area_penalty = abs(math.log(max(1e-6, target.area_normalized / source.area_normalized))) * Config.CROSS_AREA_LOG_WEIGHT
        sx, sy = source.center_normalized
        tx, ty = target.center_normalized
        position_penalty = math.hypot(sx - tx, sy - ty) * Config.CROSS_POSITION_WEIGHT
        return dt + area_penalty + position_penalty

    def update_camera_tracks(self, camera_id: int, tracks: Sequence[Track]) -> None:
        self.history[camera_id] = {t.track_id: t for t in tracks if not t.matched and not t.pending}

    def match(self, target_camera: int, target_tracks: Sequence[Track], frame: Optional[np.ndarray]) -> List[RawTransition]:
        candidates: List[Tuple[float, str, str, Track, Track]] = []
        for target in target_tracks:
            for source_camera, source_tracks in self.history.items():
                if source_camera == target_camera:
                    continue
                for source in source_tracks.values():
                    if source.matched or source.pending:
                        continue
                    cost = self.cost(source, target)
                    if cost is not None and cost <= Config.CROSS_MAX_COST:
                        candidates.append((cost, source.track_id, target.track_id, source, target))
        # Максимизируем число пар, затем минимизируем суммарную стоимость.
        # Жадный nearest может оставить катушку без пары в плотном потоке.
        sources = sorted({item[3].track_id: item[3] for item in candidates}.values(), key=lambda t: t.track_id)
        targets = sorted({item[4].track_id: item[4] for item in candidates}.values(), key=lambda t: t.track_id)
        source_index = {t.track_id: i for i, t in enumerate(sources)}
        target_index = {t.track_id: i for i, t in enumerate(targets)}
        costs = [(cost, source_index[source_id], target_index[target_id]) for cost, source_id, target_id, _s, _t in candidates]
        result: List[RawTransition] = []
        for source_idx, target_idx in minimum_cost_bipartite_pairs(len(sources), len(targets), costs):
            source = sources[source_idx]
            target = targets[target_idx]
            source.pending = True
            target.pending = True
            direction = f"{source.camera_id}>{target.camera_id}"
            result.append(
                RawTransition(
                    direction=direction,
                    from_camera=source.camera_id,
                    to_camera=target.camera_id,
                    started_at=source.first_seen,
                    captured_at=target.first_seen,
                    processed_at=datetime.now(),
                    time_diff_sec=(target.first_seen - source.last_seen).total_seconds(),
                    transport=merge_context(source.context, target.context),
                    source_track_id=source.track_id,
                    target_track_id=target.track_id,
                    image=frame.copy() if frame is not None else None,
                    source_track=source,
                    target_track=target,
                )
            )
        result.sort(key=lambda x: (x.captured_at, x.source_track_id, x.target_track_id))
        return result


class TransitionBatcher:
    def __init__(self, window_sec: float) -> None:
        self.window_sec = window_sec
        self.pending: List[RawTransition] = []

    def add(self, transitions: Iterable[RawTransition]) -> None:
        self.pending.extend(transitions)
        self.pending.sort(key=lambda x: x.captured_at)

    def flush_ready(self, now: datetime, force: bool = False) -> List[GroupedTransition]:
        if not self.pending:
            return []
        self.pending.sort(key=lambda x: (x.captured_at, x.direction, x.source_track_id))
        clusters: List[List[RawTransition]] = []
        for transition in self.pending:
            if not clusters:
                clusters.append([transition])
                continue
            last = clusters[-1]
            same_direction = last[0].direction == transition.direction
            near_anchor = abs((transition.captured_at - last[0].captured_at).total_seconds()) <= self.window_sec
            if same_direction and near_anchor:
                last.append(transition)
            else:
                clusters.append([transition])

        ready_groups: List[List[RawTransition]] = []
        waiting: List[RawTransition] = []
        for members in clusters:
            # Группа готова только когда окно прошло после ПОСЛЕДНЕГО её члена.
            # Иначе первая катушка выгружалась отдельно до прихода второй.
            latest = max(x.captured_at for x in members)
            if force or (now - latest).total_seconds() >= self.window_sec:
                ready_groups.append(members)
            else:
                waiting.extend(members)
        self.pending = waiting

        output: List[GroupedTransition] = []
        for members in ready_groups:
            image = members[-1].image
            output.append(
                GroupedTransition(
                    event_uuid=str(uuid.uuid4()),
                    direction=members[0].direction,
                    from_camera=members[0].from_camera,
                    to_camera=members[0].to_camera,
                    captured_at=max(x.captured_at for x in members),
                    processed_at=max(x.processed_at for x in members),
                    time_diff_sec=sum(x.time_diff_sec for x in members) / len(members),
                    transport=merge_context_many([x.transport for x in members]),
                    reel_count=len(members),
                    source_track_ids=[f"{x.source_track_id}>{x.target_track_id}" for x in members],
                    image_bytes=encode_jpeg(image),
                    track_refs=[t for x in members for t in (x.source_track, x.target_track)],
                )
            )
        return output



class DurableEventSpool:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """
CREATE TABLE IF NOT EXISTS events(
  event_uuid TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  image_blob BLOB,
  state TEXT NOT NULL DEFAULT 'PENDING',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt REAL NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT
)
"""
            )
        self.maintenance()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def maintenance(self) -> None:
        cutoff = (datetime.now() - timedelta(days=max(1, Config.SPOOL_RETENTION_DAYS))).isoformat()
        try:
            with self.lock, self._connect() as conn:
                conn.execute("DELETE FROM events WHERE state='SENT' AND sent_at IS NOT NULL AND sent_at<?", (cutoff,))
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            size_mb = self.path.stat().st_size / (1024 * 1024) if self.path.exists() else 0.0
            if size_mb >= Config.SPOOL_WARN_MB:
                log.critical("Video spool %.1f MB превышает порог %s MB; pending события не удаляются", size_mb, Config.SPOOL_WARN_MB)
        except Exception:
            log.exception("Video spool maintenance failed")

    def enqueue(self, event: GroupedTransition) -> bool:
        payload = {
            "event_uuid": event.event_uuid,
            "direction": event.direction,
            "from_camera": event.from_camera,
            "to_camera": event.to_camera,
            "captured_at": event.captured_at.isoformat(),
            "processed_at": event.processed_at.isoformat(),
            "time_diff_sec": event.time_diff_sec,
            "transport": event.transport,
            "reel_count": event.reel_count,
            "source_track_ids": event.source_track_ids,
        }
        try:
            inserted = False
            with self.lock, self._connect() as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO events(event_uuid,payload_json,image_blob,created_at) VALUES(?,?,?,?)",
                    (event.event_uuid, json.dumps(payload, ensure_ascii=False), event.image_bytes, datetime.now().isoformat()),
                )
                inserted = cur.rowcount > 0
                conn.commit()
            if inserted:
                append_csv(event)
            return True
        except Exception:
            log.exception("Не удалось durable-enqueue video event")
            return False

    def next_pending(self) -> Optional[Tuple[str, dict, Optional[bytes], int]]:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT event_uuid,payload_json,image_blob,attempts FROM events WHERE state='PENDING' AND next_attempt<=? ORDER BY created_at LIMIT 1",
                (time.time(),),
            ).fetchone()
        if not row:
            return None
        return row[0], json.loads(row[1]), row[2], int(row[3])

    def mark_sent(self, event_uuid: str) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("UPDATE events SET state='SENT',sent_at=?,last_error=NULL WHERE event_uuid=?", (datetime.now().isoformat(), event_uuid))
            conn.commit()

    def mark_failed(self, event_uuid: str, attempts: int, error: str) -> None:
        delay = min(Config.RETRY_MAX_SEC, Config.RETRY_BASE_SEC * (2 ** min(attempts, 8)))
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE events SET attempts=?,next_attempt=?,last_error=? WHERE event_uuid=?",
                (attempts + 1, time.time() + delay, error[:1000], event_uuid),
            )
            conn.commit()

    def stats(self) -> Tuple[int, int, int]:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT SUM(CASE WHEN state='PENDING' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN state='SENT' THEN 1 ELSE 0 END), COUNT(*) FROM events"
            ).fetchone()
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


class DBWriter(threading.Thread):
    def __init__(self, spool: DurableEventSpool) -> None:
        super().__init__(name="video-db-writer", daemon=True)
        self.spool = spool
        self.running = True
        self.sent_since_maintenance = 0
        self.delivered_total = 0
        self.failed_total = 0
        self.last_success_at: Optional[datetime] = None

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        while self.running:
            item = self.spool.next_pending()
            if item is None:
                time.sleep(0.25)
                continue
            event_uuid, payload, image, attempts = item
            try:
                self.insert(payload, image)
                self.spool.mark_sent(event_uuid)
                self.delivered_total += 1
                self.last_success_at = datetime.now()
                self.sent_since_maintenance += 1
                if self.sent_since_maintenance >= 500:
                    self.spool.maintenance()
                    self.sent_since_maintenance = 0
                log.info("Видео записано: %s, reels=%s", event_uuid, payload["reel_count"])
            except Exception as exc:
                self.failed_total += 1
                log.warning("DB video retry %s: %s", attempts + 1, exc)
                self.spool.mark_failed(event_uuid, attempts, str(exc))

    @staticmethod
    def insert(payload: dict, image: Optional[bytes]) -> None:
        if not Config.DB_CONN:
            raise RuntimeError("RFID_DB_CONNECTION не задан")
        import pyodbc
        with pyodbc.connect(Config.DB_CONN, autocommit=False, timeout=10) as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
IF NOT EXISTS(SELECT 1 FROM {Config.DB_TABLE} WHERE ClientEventUuid=?)
BEGIN
  INSERT INTO {Config.DB_TABLE}(
    [Timestamp],Direction,FromCamera,ToCamera,TransportMode,TimeDiffSec,
    ImageData,ImageSourceCamera,ImageFormat,DetectionCount,Notes,
    ClientEventUuid,CapturedAt,ProcessedAt,ReceivedAt,ReelCount,SourceTrackIds,TimeQuality
  ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
END
""",
                payload["event_uuid"],
                datetime.fromisoformat(payload["captured_at"]),
                payload["direction"],
                payload["from_camera"],
                payload["to_camera"],
                payload["transport"],
                round(float(payload["time_diff_sec"]), 3),
                image,
                payload["to_camera"],
                "jpg" if image else None,
                int(payload["reel_count"]),
                "v3 durable queue",
                payload["event_uuid"],
                datetime.fromisoformat(payload["captured_at"]),
                datetime.fromisoformat(payload["processed_at"]),
                datetime.now(),
                int(payload["reel_count"]),
                json.dumps(payload["source_track_ids"], ensure_ascii=False),
                "CAPTURE_TIMESTAMP",
            )
            conn.commit()


def encode_jpeg(frame: Optional[np.ndarray]) -> Optional[bytes]:
    if frame is None:
        return None
    h, w = frame.shape[:2]
    if w > Config.IMAGE_MAX_WIDTH or h > Config.IMAGE_MAX_HEIGHT:
        scale = min(Config.IMAGE_MAX_WIDTH / w, Config.IMAGE_MAX_HEIGHT / h)
        frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, Config.IMAGE_QUALITY])
    return buf.tobytes() if ok else None


def merge_context_many(values: Sequence[str]) -> str:
    result = "alone"
    for value in values:
        result = merge_context(result, value)
    return result


def merge_context(a: str, b: str) -> str:
    values = {a, b}
    if "forklift+human" in values or ("forklift" in values and "human" in values):
        return "forklift+human"
    if "forklift" in values:
        return "forklift"
    if "human" in values:
        return "human"
    return "alone"


def center_distance(a: Detection, b: Detection) -> float:
    ac, bc = a.center, b.center
    return math.hypot(ac[0] - bc[0], ac[1] - bc[1])


def context_for_reel(reel: Detection, detections: Sequence[Detection]) -> str:
    nearby = {
        d.class_name
        for d in detections
        if d.class_name in {"forklift", "human"} and center_distance(reel, d) <= Config.CONTEXT_DISTANCE_PX
    }
    if {"forklift", "human"}.issubset(nearby):
        return "forklift+human"
    if "forklift" in nearby:
        return "forklift"
    if "human" in nearby:
        return "human"
    return "alone"


def append_csv(event: GroupedTransition) -> None:
    path = Path(Config.CSV_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["event_uuid", "captured_at", "direction", "from_camera", "to_camera", "transport", "reel_count", "time_diff_sec", "tracks"])
        writer.writerow([event.event_uuid, event.captured_at.isoformat(), event.direction, event.from_camera, event.to_camera, event.transport, event.reel_count, f"{event.time_diff_sec:.3f}", json.dumps(event.source_track_ids)])


def load_mask(camera_id: int, frame_shape: Tuple[int, int, int], cache: Dict[int, np.ndarray]) -> Optional[np.ndarray]:
    if not Config.MASK_ENABLED:
        return None
    path = Config.MASKS.get(camera_id, "")
    if not path or not Path(path).exists():
        return None
    if camera_id not in cache:
        mask = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if mask is None:
            return None
        cache[camera_id] = mask
    mask = cache[camera_id]
    h, w = frame_shape[:2]
    return cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST) if mask.shape != (h, w) else mask


def allowed(mask: Optional[np.ndarray], bbox: Tuple[int, int, int, int]) -> bool:
    if mask is None:
        return True
    x1, y1, x2, y2 = bbox
    h, w = mask.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return False
    roi = mask[y1:y2, x1:x2]
    return bool(roi.size and np.count_nonzero(roi > 127) / roi.size >= 0.3)


def detect(model, frame: np.ndarray, camera_id: int, captured_at: datetime, mask_cache: Dict[int, np.ndarray]) -> Tuple[np.ndarray, List[Detection]]:
    output = frame.copy()
    mask = load_mask(camera_id, frame.shape, mask_cache)
    results = model.predict(source=frame, conf=Config.CONF, iou=Config.IOU, verbose=False, device=Config.DEVICE)
    detections: List[Detection] = []
    for result in results:
        for box in result.boxes:
            cls_id = int(box.cls)
            name = str(model.names[cls_id])
            bbox = tuple(map(int, box.xyxy[0]))
            if not allowed(mask, bbox):
                continue
            det = Detection(name, float(box.conf), bbox)
            detections.append(det)
            x1, y1, x2, y2 = bbox
            cv2.rectangle(output, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(output, f"{name} {det.confidence:.2f}", (x1, max(20, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
    return output, detections


def retry_undurable_events(
    spool: DurableEventSpool, events: Sequence[GroupedTransition]
) -> List[GroupedTransition]:
    remaining: List[GroupedTransition] = []
    for event in events:
        if spool.enqueue(event):
            log.info(
                "ПЕРЕХОД В ОЧЕРЕДИ: %s camera=%s>%s reels=%s transport=%s dt=%.2fs captured=%s",
                event.direction, event.from_camera, event.to_camera, event.reel_count, event.transport,
                event.time_diff_sec, event.captured_at.strftime("%H:%M:%S.%f")[:-3],
            )
            for track in event.track_refs:
                track.pending = False
                track.matched = True
        else:
            remaining.append(event)
    return remaining


def main() -> int:
    instance_lock = SingleInstanceLock(Config.LOCK_PATH)
    if not Config.DB_CONN:
        raise RuntimeError("Не задан RFID_DB_CONNECTION; работа только в локальный spool без оператора запрещена")
    if not Path(Config.MODEL_PATH).exists():
        raise FileNotFoundError(Config.MODEL_PATH)
    if len(Config.CAMERA_IDS) != len(Config.RTSP_URLS):
        raise ValueError("RFID_CAMERA_IDS и RFID_RTSP_* не совпадают")
    from ultralytics import YOLO

    model = YOLO(Config.MODEL_PATH)
    model_classes = {str(name) for name in model.names.values()} if isinstance(model.names, dict) else {str(name) for name in model.names}
    if Config.REEL_CLASS not in model_classes:
        raise RuntimeError(
            f"Класс катушки {Config.REEL_CLASS!r} отсутствует в модели. Доступны: {sorted(model_classes)}"
        )
    log.info("Катушкой в YOLO считается только класс %s", Config.REEL_CLASS)
    streams = [RTSPStream(url, cid).start() for cid, url in zip(Config.CAMERA_IDS, Config.RTSP_URLS)]
    trackers = {cid: CentroidTracker(cid) for cid in Config.CAMERA_IDS}
    matcher = CrossCameraMatcher(Config.CAMERA_IDS)
    batcher = TransitionBatcher(Config.GROUP_WINDOW_SEC)
    spool = DurableEventSpool(Config.SPOOL_PATH)
    writer = DBWriter(spool)
    writer.start()
    last_sequences = {cid: 0 for cid in Config.CAMERA_IDS}
    mask_cache: Dict[int, np.ndarray] = {}
    undurable_events: List[GroupedTransition] = []
    frame_counts = {cid: 0 for cid in Config.CAMERA_IDS}
    detection_counts = {cid: 0 for cid in Config.CAMERA_IDS}
    reel_counts = {cid: 0 for cid in Config.CAMERA_IDS}
    last_capture = {cid: None for cid in Config.CAMERA_IDS}
    transitions_total = 0
    last_heartbeat = time.monotonic()
    try:
        while True:
            processed_any = False
            for stream in streams:
                packet = stream.read_new(last_sequences[stream.camera_id])
                if packet is None:
                    continue
                processed_any = True
                last_sequences[stream.camera_id] = packet.sequence
                frame_counts[stream.camera_id] += 1
                last_capture[stream.camera_id] = packet.captured_at
                shown, detections = detect(model, packet.frame, stream.camera_id, packet.captured_at, mask_cache)
                detection_counts[stream.camera_id] += len(detections)
                reel_dets = [d for d in detections if d.class_name == Config.REEL_CLASS]
                reel_counts[stream.camera_id] += len(reel_dets)
                tracks = trackers[stream.camera_id].update(reel_dets, packet.captured_at, packet.frame.shape[:2])
                for track in tracks:
                    nearest = min(reel_dets, key=lambda d: CentroidTracker.distance(track.bbox, d.bbox), default=None)
                    if nearest:
                        track.context = context_for_reel(nearest, detections)
                transitions = matcher.match(stream.camera_id, tracks, packet.frame)
                transitions_total += len(transitions)
                batcher.add(transitions)
                matcher.update_camera_tracks(stream.camera_id, tracks)
                if not Config.HEADLESS:
                    cv2.putText(shown, f"capture {packet.captured_at:%H:%M:%S.%f}"[:-3], (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
                    cv2.imshow(f"Camera {stream.camera_id}", cv2.resize(shown, (Config.WINDOW_WIDTH, Config.WINDOW_HEIGHT)))
            undurable_events.extend(batcher.flush_ready(datetime.now()))
            if undurable_events:
                undurable_events = retry_undurable_events(spool, undurable_events)
                if undurable_events:
                    log.critical("Не записано в local video spool: %s событий; retry продолжается", len(undurable_events))
            now_mono = time.monotonic()
            if now_mono - last_heartbeat >= Config.HEARTBEAT_SEC:
                pending, sent, total = spool.stats()
                camera_parts = []
                for cid in Config.CAMERA_IDS:
                    age = "none" if last_capture[cid] is None else f"{(datetime.now()-last_capture[cid]).total_seconds():.1f}s"
                    camera_parts.append(
                        f"cam{cid}:frames={frame_counts[cid]},det={detection_counts[cid]},reels={reel_counts[cid]},age={age}"
                    )
                db_age = "never" if writer.last_success_at is None else f"{(datetime.now()-writer.last_success_at).total_seconds():.1f}s"
                log.info(
                    "STATUS %s transitions=%s group_wait=%s local_retry=%s spool_pending=%s spool_sent=%s db_delivered=%s db_last_ok=%s db_errors=%s",
                    " | ".join(camera_parts), transitions_total, len(batcher.pending), len(undurable_events),
                    pending, sent, writer.delivered_total, db_age, writer.failed_total,
                )
                frame_counts = {cid: 0 for cid in Config.CAMERA_IDS}
                detection_counts = {cid: 0 for cid in Config.CAMERA_IDS}
                reel_counts = {cid: 0 for cid in Config.CAMERA_IDS}
                last_heartbeat = now_mono
            if not Config.HEADLESS and cv2.waitKey(1) & 0xFF == ord("q"):
                break
            if not processed_any:
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        undurable_events.extend(batcher.flush_ready(datetime.now(), force=True))
        shutdown_deadline = time.monotonic() + 10.0
        while undurable_events and time.monotonic() < shutdown_deadline:
            undurable_events = retry_undurable_events(spool, undurable_events)
            if undurable_events:
                time.sleep(0.5)
        if undurable_events:
            log.critical("При остановке не удалось durable-enqueue %s video events", len(undurable_events))
        writer.stop()
        writer.join(timeout=5)
        for stream in streams:
            stream.stop()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
