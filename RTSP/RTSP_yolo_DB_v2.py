#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RTSP + YOLO + DB (improved)

Что улучшено:
- исправлена работа с pyodbc connection pool
- используются CAMERA_IDS и env-настройки трекера
- единый формат направления 0>1 / 1>0
- трекер больше не держит только одну последнюю катушку
- уменьшается шанс потери событий при нескольких катушках подряд
"""
import cv2
import threading
import time
import os
import csv
import pyodbc
from datetime import datetime
from collections import deque
import numpy as np

DB_CONNECTION_STRING = os.getenv("RFID_DB_CONNECTION", "")
DB_LOG_TABLE = os.getenv("RFID_DB_LOG_TABLE", "ReelTransitions")
SAVE_IMAGE_ON_TRANSITION = os.getenv("RFID_SAVE_IMAGE_ON_TRANSITION", "True").lower() in ("true", "1", "yes")
IMAGE_QUALITY = int(os.getenv("RFID_IMAGE_QUALITY", "85"))
IMAGE_MAX_WIDTH = int(os.getenv("RFID_IMAGE_MAX_WIDTH", "640"))
IMAGE_MAX_HEIGHT = int(os.getenv("RFID_IMAGE_MAX_HEIGHT", "480"))

MASK_ENABLED = os.getenv("RFID_MASK_ENABLED", "True").lower() in ("true", "1", "yes")
MASK_PATHS = {
    0: os.getenv("RFID_MASK_0", r"C:\Users\perestoroninAM\Desktop\RFID\RTSP\Mask_0.jpg"),
    1: os.getenv("RFID_MASK_1", r"C:\Users\perestoroninAM\Desktop\RFID\RTSP\Mask_1.jpg"),
}
SHOW_MASK_OVERLAY = os.getenv("RFID_SHOW_MASK_OVERLAY", "False").lower() in ("true", "1", "yes")
mask_cache = {}

MODEL_PATH = os.getenv("RFID_MODEL_PATH", "runs/detect/rfid_forklift_reel2/weights/best.pt")
CONFIDENCE_THRESHOLD = float(os.getenv("RFID_CONFIDENCE_THRESHOLD", "0.25"))
IOU_THRESHOLD = float(os.getenv("RFID_IOU_THRESHOLD", "0.45"))
ENABLE_DETECTION_BY_DEFAULT = os.getenv("RFID_ENABLE_DETECTION", "True").lower() in ("true", "1", "yes")
LOG_DETECTIONS = os.getenv("RFID_LOG_DETECTIONS", "True").lower() in ("true", "1", "yes")
LOG_THROTTLE_MS = int(os.getenv("RFID_LOG_THROTTLE_MS", "500"))
CSV_LOG_PATH = os.getenv("RFID_CSV_LOG_PATH", "recordings/detections_log.csv")

USERNAME = os.getenv("RFID_CAMERA_USER", "Akim")
PASSWORD = os.getenv("RFID_CAMERA_PASS", "MylenE12")
RTSP_URLS = [
    os.getenv("RFID_RTSP_0", f"rtsp://{USERNAME}:{PASSWORD}@10.192.2.11:554/Streaming/Channels/101"),
    os.getenv("RFID_RTSP_1", f"rtsp://{USERNAME}:{PASSWORD}@10.192.2.12:554/Streaming/Channels/101"),
]
CAMERA_IDS = [int(os.getenv("RFID_CAMERA_0_ID", "0")), int(os.getenv("RFID_CAMERA_1_ID", "1"))]

REEL_TRACKING_ENABLED = os.getenv("RFID_REEL_TRACKING_ENABLED", "True").lower() in ("true", "1", "yes")
REEL_CLASS_NAME = os.getenv("RFID_REEL_CLASS_NAME", "cable_reel")
TRANSITION_WINDOW_SEC = float(os.getenv("RFID_TRANSITION_WINDOW_SEC", "30.0"))
REEL_DISAPPEAR_SEC = float(os.getenv("RFID_REEL_DISAPPEAR_SEC", "5.0"))
REEL_NEARBY_THRESHOLD_PX = int(os.getenv("RFID_REEL_NEARBY_THRESHOLD_PX", "400"))
REEL_TRANSITION_LOG_PATH = os.getenv("RFID_REEL_TRANSITION_LOG_PATH", "recordings/reel_transitions.csv")
TRACK_MERGE_DISTANCE_PX = int(os.getenv("RFID_TRACK_MERGE_DISTANCE_PX", "120"))
EVENT_COOLDOWN_SEC = float(os.getenv("RFID_EVENT_COOLDOWN_SEC", "2.0"))

WINDOW_WIDTH = int(os.getenv("RFID_WINDOW_WIDTH", "640"))
WINDOW_HEIGHT = int(os.getenv("RFID_WINDOW_HEIGHT", "480"))

active_camera_id = None
detection_enabled = ENABLE_DETECTION_BY_DEFAULT
log_enabled = LOG_DETECTIONS
last_log_time = {}
model = None
csv_file_initialized = False
db_pool = []
db_lock = threading.Lock()


def is_conn_alive(conn) -> bool:
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        return True
    except Exception:
        return False

def get_db_conn():
    if not DB_CONNECTION_STRING:
        return None
    with db_lock:
        while db_pool:
            conn = db_pool.pop()
            if is_conn_alive(conn):
                return conn
            try:
                conn.close()
            except Exception:
                pass
    try:
        return pyodbc.connect(DB_CONNECTION_STRING, timeout=5)
    except Exception as e:
        print(f"✗ БД ошибка: {e}")
        return None

def return_db_conn(conn):
    if not conn:
        return
    if is_conn_alive(conn):
        with db_lock:
            db_pool.append(conn)
    else:
        try:
            conn.close()
        except Exception:
            pass

def frame_to_bytes(frame, quality=85, max_w=640, max_h=480):
    try:
        h, w = frame.shape[:2]
        if w > max_w or h > max_h:
            scale = min(max_w / w, max_h / h)
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LINEAR)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None
    except Exception as e:
        print(f"✗ Ошибка кодирования: {e}")
        return None

def log_to_db(direction, from_cam, to_cam, transport, time_diff, img_bytes, src_cam, det_cnt=0, notes=None):
    if not DB_CONNECTION_STRING or not SAVE_IMAGE_ON_TRANSITION:
        return False
    conn = get_db_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(f"""
            INSERT INTO {DB_LOG_TABLE} (
                Timestamp, Direction, FromCamera, ToCamera, TransportMode,
                TimeDiffSec, ImageBase64, ImageSourceCamera, ImageFormat,
                DetectionCount, Notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, datetime.now(), direction, from_cam, to_cam, transport,
             round(time_diff, 2), img_bytes, src_cam, "jpg", det_cnt, notes)
        conn.commit()
        cur.close()
        print(f"✓ БД: {direction} | {transport}")
        return True
    except Exception as e:
        print(f"✗ БД записи ошибка: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        return_db_conn(conn)

def load_mask(cam_id, shape):
    if not MASK_ENABLED or cam_id not in MASK_PATHS:
        return None
    path = MASK_PATHS[cam_id]
    if not os.path.exists(path):
        print(f"⚠️ Маска не найдена: {path}")
        return None
    if cam_id not in mask_cache:
        m = cv2.imread(path)
        if m is None:
            return None
        if m.ndim == 3:
            m = cv2.cvtColor(m, cv2.COLOR_BGR2GRAY)
        mask_cache[cam_id] = m
    mask = mask_cache[cam_id]
    h, w = shape[:2]
    if mask.shape != (h, w):
        mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
    return mask

def is_allowed(mask, box, min_ratio=0.3):
    if mask is None:
        return True
    if mask.ndim == 3:
        mask = mask[:, :, 0]
    if mask.dtype != np.uint8:
        mask = mask.astype(np.uint8)
    x1, y1, x2, y2 = map(int, box)
    h, w = mask.shape
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return False
    roi = mask[y1:y2, x1:x2]
    return np.count_nonzero(roi > 127) / roi.size >= min_ratio if roi.size > 0 else False

def mouse_cb(event, x, y, flags, param):
    global active_camera_id
    if event == cv2.EVENT_MOUSEMOVE:
        active_camera_id = param

def init_csv():
    global csv_file_initialized
    if not csv_file_initialized:
        folder = os.path.dirname(CSV_LOG_PATH)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if not os.path.exists(CSV_LOG_PATH):
            with open(CSV_LOG_PATH, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["timestamp", "camera_id", "class_id", "class_name", "confidence", "x1", "y1", "x2", "y2", "width", "height", "center_x", "center_y"])
        csv_file_initialized = True

def log_csv(cam_id, cls_id, cls_name, conf, box):
    init_csv()
    x1, y1, x2, y2 = map(int, box)
    with open(CSV_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([datetime.now().isoformat(), cam_id, cls_id, cls_name, f"{conf:.3f}", x1, y1, x2, y2, x2 - x1, y2 - y1, (x1 + x2) // 2, (y1 + y2) // 2])

def print_log(cam_id, cls_name, conf, box, shape):
    global last_log_time
    now = time.time() * 1000
    if not log_enabled or (now - last_log_time.get(cam_id, 0)) < LOG_THROTTLE_MS:
        return
    last_log_time[cam_id] = now

def draw_det(frame, cls_name, conf, box, color=(0, 255, 0)):
    x1, y1, x2, y2 = map(int, box)
    lbl = f"{cls_name} {conf:.2f}"
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    tw, _ = cv2.getTextSize(lbl, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)[0]
    cv2.rectangle(frame, (x1, y1 - 25), (x1 + tw, y1), color, -1)
    cv2.putText(frame, lbl, (x1, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
    cv2.circle(frame, ((x1 + x2) // 2, (y1 + y2) // 2), 4, (255, 0, 0), -1)
    return frame

def save_frame(frame, cam_id, out_dir="recordings", prefix="manual"):
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = f"{out_dir}/{prefix}_cam{cam_id}_{ts}.jpg"
    cv2.imwrite(path, frame)
    print(f"✓ Сохранено: {path}")
    return path

def process_frame(frame, cam_id):
    global model
    if not detection_enabled or model is None:
        return frame, []
    try:
        results = model.predict(source=frame, conf=CONFIDENCE_THRESHOLD, iou=IOU_THRESHOLD, verbose=False, device="cpu")
        dets = []
        out = frame.copy()
        for r in results:
            for box in r.boxes:
                cid = int(box.cls)
                conf = float(box.conf)
                name = model.names[cid]
                x1, y1, x2, y2 = map(int, box.xyxy[0])

                if MASK_ENABLED:
                    m = load_mask(cam_id, frame.shape)
                    if not is_allowed(m, (x1, y1, x2, y2)):
                        continue

                dets.append({
                    "camera_id": cam_id,
                    "class_id": cid,
                    "class_name": name,
                    "confidence": conf,
                    "bbox": (x1, y1, x2, y2),
                    "timestamp": datetime.now(),
                })
                col = (0, 255, 0) if name == "forklift" else (255, 100, 0)
                out = draw_det(out, name, conf, (x1, y1, x2, y2), col)
                if log_enabled:
                    print_log(cam_id, name, conf, (x1, y1, x2, y2), frame.shape)
                    log_csv(cam_id, cid, name, conf, (x1, y1, x2, y2))
        return out, dets
    except Exception as e:
        print(f"✗ YOLO ошибка: {e}")
        return frame, []


class RTSPStream:
    def __init__(self, url, cam_id=0, buf_size=1):
        self.url = url
        self.cam_id = cam_id
        self.buf_size = buf_size
        self.frames = []
        self.lock = threading.Lock()
        self.running = False
        self.cap = None
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        print(f"[Cam {self.cam_id}] Старт: {self.url}")
        return self

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        if self.cap:
            self.cap.release()
        print(f"[Cam {self.cam_id}] Стоп")

    def _loop(self):
        while self.running:
            try:
                if self.cap:
                    try:
                        self.cap.release()
                    except Exception:
                        pass
                self.cap = cv2.VideoCapture(self.url)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not self.cap.isOpened():
                    raise ConnectionError("Не удалось открыть")
                print(f"[Cam {self.cam_id}] ✓ Подключено")
                while self.running:
                    ret, fr = self.cap.read()
                    if ret:
                        with self.lock:
                            if len(self.frames) >= self.buf_size:
                                self.frames.pop(0)
                            self.frames.append(fr.copy())
                    else:
                        print(f"[Cam {self.cam_id}] ⚠ Переподключение...")
                        break
            except Exception as e:
                print(f"[Cam {self.cam_id}] ✗ {e}")
                time.sleep(5)

    def read(self):
        with self.lock:
            return self.frames[-1].copy() if self.frames else None

    def get_latest_frame(self):
        return self.read()


class MultiReelTracker:
    def __init__(self, cam_ids, time_win=30.0, prox=400, history_sec=2.0, merge_dist=120, cooldown_sec=2.0):
        self.cam_ids = cam_ids
        self.time_win = time_win
        self.prox = prox
        self.history_sec = history_sec
        self.merge_dist = merge_dist
        self.cooldown_sec = cooldown_sec
        self.object_history = {cid: [] for cid in cam_ids}
        self.reel_sightings = {cid: deque(maxlen=200) for cid in cam_ids}
        self._last_event_ts = 0.0
        self._seq = 0

    def _center(self, bbox):
        return ((bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2)

    def _dist(self, box_a, box_b):
        if not box_a or not box_b:
            return float("inf")
        x1, y1 = self._center(box_a)
        x2, y2 = self._center(box_b)
        return ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5

    def _cleanup(self, current_time):
        for cam_id in self.cam_ids:
            self.object_history[cam_id] = [
                (t, b, c) for t, b, c in self.object_history[cam_id]
                if current_time - t <= self.history_sec
            ]
            while self.reel_sightings[cam_id] and current_time - self.reel_sightings[cam_id][0]["last_seen"] > self.time_win:
                self.reel_sightings[cam_id].popleft()

    def _nearby(self, reel_box, cam_id, current_dets, current_time):
        nearby_candidates = []
        for d in current_dets:
            if d["class_name"] in ("forklift", "human") and self._dist(reel_box, d["bbox"]) < self.prox:
                nearby_candidates.append(d["class_name"])
        for ts, bbox, cls_name in self.object_history[cam_id]:
            if current_time - ts <= self.history_sec and self._dist(reel_box, bbox) < self.prox * 1.5:
                nearby_candidates.append(cls_name)

        nearby_candidates = list(set(nearby_candidates))
        if "forklift" in nearby_candidates and "human" in nearby_candidates:
            return "forklift+human"
        if "forklift" in nearby_candidates:
            return "forklift"
        if "human" in nearby_candidates:
            return "human"
        return "alone"

    def _upsert_same_cam_sighting(self, cam_id, bbox, nearby, current_time):
        for item in reversed(self.reel_sightings[cam_id]):
            if current_time - item["last_seen"] > self.history_sec:
                continue
            if self._dist(item["bbox"], bbox) <= self.merge_dist:
                item["bbox"] = bbox
                item["last_seen"] = current_time
                item["nearby"] = nearby
                return item
        self._seq += 1
        item = {
            "id": self._seq,
            "camera": cam_id,
            "first_seen": current_time,
            "last_seen": current_time,
            "bbox": bbox,
            "nearby": nearby,
            "matched": False,
        }
        self.reel_sightings[cam_id].append(item)
        return item

    def _best_cross_cam_match(self, cam_id, bbox, current_time):
        candidates = []
        for other_cam in self.cam_ids:
            if other_cam == cam_id:
                continue
            for item in self.reel_sightings[other_cam]:
                if item["matched"]:
                    continue
                dt = current_time - item["last_seen"]
                if dt < 0 or dt > self.time_win:
                    continue
                candidates.append((abs(dt), item))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def _normalize_direction(self, from_cam, to_cam):
        if from_cam == 0 and to_cam == 1:
            return "0>1"
        if from_cam == 1 and to_cam == 0:
            return "1>0"
        return f"{from_cam}>{to_cam}"

    def _log_event(self, direction, transport, from_c, to_c, tdiff, frame, src_cam, det_cnt):
        if time.time() - self._last_event_ts < self.cooldown_sec:
            return
        self._last_event_ts = time.time()

        emoji = {"forklift": "🚜", "human": "🚶", "forklift+human": "🚜+🚶", "alone": "📦"}
        print(f"\n[{'='*70}]\n🎞️ ПЕРЕХОД КАТУШКИ\n🧭 {direction} (камера {from_c} → {to_c})\n🕐 {tdiff:.1f} сек\n🔧 {transport} {emoji.get(transport,'')}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n[{'='*70}]\n")

        try:
            folder = os.path.dirname(REEL_TRANSITION_LOG_PATH)
            if folder:
                os.makedirs(folder, exist_ok=True)
            exists = os.path.exists(REEL_TRANSITION_LOG_PATH)
            with open(REEL_TRANSITION_LOG_PATH, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if not exists:
                    w.writerow(["timestamp", "direction", "from_camera", "to_camera", "transport_mode", "time_diff_sec"])
                w.writerow([datetime.now().isoformat(), direction, from_c, to_c, transport, f"{tdiff:.1f}"])
        except Exception as e:
            print(f"⚠️ CSV ошибка: {e}")

        if SAVE_IMAGE_ON_TRANSITION and frame is not None:
            img_bytes = frame_to_bytes(frame, IMAGE_QUALITY, IMAGE_MAX_WIDTH, IMAGE_MAX_HEIGHT)
            if img_bytes:
                log_to_db(direction, from_c, to_c, transport, tdiff, img_bytes, src_cam, det_cnt)

    def update(self, cam_id, all_dets, frame=None, current_time=None):
        if current_time is None:
            current_time = time.time()

        self._cleanup(current_time)

        for d in all_dets:
            if d["class_name"] in ("forklift", "human"):
                self.object_history[cam_id].append((current_time, d["bbox"], d["class_name"]))

        reels = [d for d in all_dets if d["class_name"] == REEL_CLASS_NAME]
        if not reels:
            return

        for reel in reels:
            bbox = reel["bbox"]
            nearby = self._nearby(bbox, cam_id, all_dets, current_time)
            current_sighting = self._upsert_same_cam_sighting(cam_id, bbox, nearby, current_time)

            match = self._best_cross_cam_match(cam_id, bbox, current_time)
            if match is None:
                continue

            direction = self._normalize_direction(match["camera"], cam_id)
            tdiff = current_time - match["last_seen"]
            transport = match["nearby"] or nearby
            self._log_event(direction, transport, match["camera"], cam_id, tdiff, frame, match["camera"], len(all_dets))
            match["matched"] = True
            current_sighting["matched"] = True


def display_streams(streams, tracker=None, win_size=(640, 480)):
    global active_camera_id, detection_enabled, log_enabled, SHOW_MASK_OVERLAY
    for s in streams:
        cv2.namedWindow(f"Camera {s.cam_id}", cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(f"Camera {s.cam_id}", mouse_cb, s.cam_id)
    print(f"\n🎮 Управление:\n  [Q] Выход | [S] Сохранить кадр под курсором | [1],[2] Сохранить с камеры\n  [D] Детекция: {'✅' if detection_enabled else '❌'} | [L] Лог: {'✅' if log_enabled else '❌'} | [M] Маска: {'✅' if SHOW_MASK_OVERLAY else '❌'}")
    if tracker:
        print(f"  🎞️ Трекинг: ✅ | 🗄️ БД: {'✅' if DB_CONNECTION_STRING else '❌'}\n")
    try:
        while True:
            for s in streams:
                fr = s.read()
                if fr is None:
                    continue
                cid = s.cam_id
                proc, dets = process_frame(fr, cid)
                ts = datetime.now().strftime("%H:%M:%S")
                status = "🔍 DETECT" if detection_enabled else "📹 LIVE"
                ind = " [←]" if cid == active_camera_id else ""
                cv2.putText(proc, f"Cam {cid} | {ts} | {status}{ind}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

                if dets:
                    cnt = {}
                    for d in dets:
                        cnt[d["class_name"]] = cnt.get(d["class_name"], 0) + 1
                    cv2.putText(proc, " | ".join(f"{k}:{v}" for k, v in cnt.items()), (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)

                if SHOW_MASK_OVERLAY and MASK_ENABLED and cid in MASK_PATHS:
                    m = load_mask(cid, fr.shape)
                    if m is not None:
                        mc = cv2.cvtColor(m, cv2.COLOR_GRAY2BGR)
                        mc[m < 127] = [0, 0, 255]
                        mc[m >= 127] = [0, 255, 0]
                        proc = cv2.addWeighted(proc, 0.7, mc, 0.3, 0)
                        cv2.putText(proc, "MASK [M]", (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

                cv2.imshow(f"Camera {cid}", cv2.resize(proc, win_size))

                if tracker and REEL_TRACKING_ENABLED:
                    src = next((st for st in streams if st.cam_id == cid), None)
                    frm = src.get_latest_frame() if src else None
                    tracker.update(cid, dets, frame=frm)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            elif key in (ord("d"), ord("D")):
                detection_enabled = not detection_enabled
            elif key in (ord("l"), ord("L")):
                log_enabled = not log_enabled
                print(f"\n📝 Лог: {'✅' if log_enabled else '❌'}\n")
            elif key in (ord("m"), ord("M")):
                SHOW_MASK_OVERLAY = not SHOW_MASK_OVERLAY
                print(f"\n🎭 Маска: {'✅' if SHOW_MASK_OVERLAY else '❌'}\n")
            elif key in (ord("s"), ord("S")):
                if active_camera_id is not None:
                    st = next((x for x in streams if x.cam_id == active_camera_id), None)
                    if st:
                        f = st.get_latest_frame()
                        if f is not None:
                            save_frame(f, active_camera_id)
            elif chr(key).isdigit() and int(chr(key)) < len(streams):
                idx = int(chr(key))
                st = streams[idx]
                f = st.get_latest_frame()
                if f is not None:
                    save_frame(f, st.cam_id)
    except KeyboardInterrupt:
        print("\n🛑 Остановка...")
    finally:
        for s in streams:
            cv2.destroyWindow(f"Camera {s.cam_id}")

def main():
    global model
    if os.path.exists(MODEL_PATH):
        print(f"🤖 Загрузка модели: {MODEL_PATH}")
        from ultralytics import YOLO
        model = YOLO(MODEL_PATH)
        print(f"✅ Модель загружена | Классы: {model.names}")
    else:
        print(f"⚠️ Модель не найдена: {MODEL_PATH}")
        return

    tracker = None
    if REEL_TRACKING_ENABLED:
        tracker = MultiReelTracker(
            cam_ids=CAMERA_IDS,
            time_win=TRANSITION_WINDOW_SEC,
            prox=REEL_NEARBY_THRESHOLD_PX,
            history_sec=REEL_DISAPPEAR_SEC,
            merge_dist=TRACK_MERGE_DISTANCE_PX,
            cooldown_sec=EVENT_COOLDOWN_SEC,
        )
        print("🎞️ Трекинг катушек: ✅")
    if DB_CONNECTION_STRING:
        print(f"🗄️ БД: ✅ ({DB_LOG_TABLE})")
    else:
        print("🗄️ БД: ❌ (не настроена)")

    streams = [RTSPStream(url, cam_id=cam_id).start() for cam_id, url in zip(CAMERA_IDS, RTSP_URLS)]
    try:
        display_streams(streams, tracker, (WINDOW_WIDTH, WINDOW_HEIGHT))
    finally:
        for s in streams:
            s.stop()
        cv2.destroyAllWindows()
        with db_lock:
            while db_pool:
                try:
                    db_pool.pop().close()
                except Exception:
                    pass
        print("👋 Готово")

if __name__ == "__main__":
    main()
