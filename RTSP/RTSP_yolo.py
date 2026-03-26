import cv2
import threading
import time
import os
import csv
from datetime import datetime
from pathlib import Path

# === НАСТРОЙКИ МАСОК ===
MASK_ENABLED = True  # Вкл/выкл фильтрацию по маске
MASK_PATHS = {
    0: r"C:\Users\perestoroninAM\Desktop\RFID\RTSP\Mask_0.jpg",  # Маска для камеры 0
    1: r"C:\Users\perestoroninAM\Desktop\RFID\RTSP\Mask_1.jpg",  # Маска для камеры 1
}
mask_cache = {}  # Кэш: {camera_id: mask_frame}
SHOW_MASK_OVERLAY = False  # Показывать маску поверх видео (отладка)

# === НАСТРОЙКИ МОДЕЛИ ===
MODEL_PATH = "runs/detect/rfid_forklift_reel2/weights/best.pt"  # Путь к вашей модели
CONFIDENCE_THRESHOLD = 0.25  # Порог уверенности для отображения
IOU_THRESHOLD = 0.45         # NMS порог
ENABLE_DETECTION_BY_DEFAULT = True  # Детекция включена при старте
LOG_DETECTIONS = True        # Логировать в консоль и файл
LOG_THROTTLE_MS = 500        # Мин. интервал между логами (мс), чтобы не спамить
CSV_LOG_PATH = "recordings/detections_log.csv"  # Файл для сохранения детекций

# === НАСТРОЙКИ КАМЕР ===
USERNAME = os.getenv("CAMERA_USER", "Akim")
PASSWORD = os.getenv("CAMERA_PASS", "MylenE12")

RTSP_URLS = [
    f"rtsp://{USERNAME}:{PASSWORD}@10.192.2.11:554/Streaming/Channels/101",
    f"rtsp://{USERNAME}:{PASSWORD}@10.192.2.12:554/Streaming/Channels/101"
]

# === НАСТРОЙКИ ТРЕКИНГА КАТУШЕК ===
REEL_TRACKING_ENABLED = True
REEL_CLASS_NAME = "reel"  # Имя класса катушки в вашей модели (проверьте model.names)
TRANSITION_CONFIRM_WINDOW_SEC = 3.0  # Окно подтверждения перехода (сек)
TRANSITION_MIN_DETECTIONS = 3        # Мин. детекций в новой зоне для подтверждения
MAX_REEL_TRACK_AGE_SEC = 10.0        # Макс. время жизни трека без обновлений
REEL_NEARBY_THRESHOLD_PX = 150       # Дистанция (пикс) для определения "рядом" с человеком/погрузчиком

# Файл для логов перемещений катушек
REEL_TRANSITION_LOG_PATH = "recordings/reel_transitions.csv"

# Глобальные переменные
active_camera_id = None
detection_enabled = ENABLE_DETECTION_BY_DEFAULT
log_enabled = LOG_DETECTIONS
last_log_time = {}  # {camera_id: timestamp} для троттлинга
model = None
csv_file_initialized = False

def load_mask(camera_id, frame_shape):
    """Загрузка и подготовка маски под размер кадра (гарантированно 2D)"""
    if not MASK_ENABLED or camera_id not in MASK_PATHS:
        return None
    
    path = MASK_PATHS[camera_id]
    if not os.path.exists(path):
        print(f"⚠️  Маска не найдена для камеры {camera_id}: {path}")
        return None
    
    # Кэшируем маску, если ещё не загружена
    if camera_id not in mask_cache:
        # Читаем как есть
        mask = cv2.imread(path)
        if mask is None:
            print(f"✗ Ошибка загрузки маски: {path}")
            return None
        
        # Конвертируем в 2D grayscale (убираем лишний канал)
        if mask.ndim == 3:
            mask = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY)
        # Теперь mask имеет размерность (H, W)
        
        mask_cache[camera_id] = mask
        print(f"✓ Маска загружена для камеры {camera_id}: {mask.shape}")
    
    # Масштабируем маску под текущий кадр
    mask = mask_cache[camera_id]
    h, w = frame_shape[:2]
    if mask.shape[0] != h or mask.shape[1] != w:
        mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
    
    return mask


def is_detection_allowed(mask, box, min_white_ratio=0.3):
    """
    Проверка: попадает ли бокс в разрешённую (белую) зону маски.
    
    Args:
        mask: grayscale маска (0=чёрный/запрет, 255=белый/разрешено), 2D массив
        box: (x1, y1, x2, y2)
        min_white_ratio: мин. доля белых пикселей в боксе для разрешения (0.0-1.0)
    
    Returns:
        bool: True если детекция разрешена
    """
    import numpy as np  # Добавляем импорт внутри функции для надёжности
    
    if mask is None:
        return True  # Нет маски = всё разрешено
    
    # Гарантия 2D и uint8
    if mask.ndim == 3:
        mask = mask[:, :, 0]
    if mask.dtype != np.uint8:
        mask = mask.astype(np.uint8)
    
    x1, y1, x2, y2 = map(int, box)
    h, w = mask.shape
    
    # Ограничиваем координаты в пределах кадра
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    
    if x2 <= x1 or y2 <= y1:
        return False
    
    # Вырезаем область маски под боксом
    roi = mask[y1:y2, x1:x2]
    
    # === ИСПРАВЛЕНИЕ: считаем белые пиксели корректно ===
    # Вариант 1: через numpy (проще и надёжнее)
    white_pixels = np.count_nonzero(roi > 127)
    
    # Вариант 2: через OpenCV (если предпочитаете)
    # white_pixels = cv2.countNonZero(cv2.inRange(roi, 128, 255))
    
    total_pixels = roi.size
    white_ratio = white_pixels / total_pixels if total_pixels > 0 else 0
    
    return white_ratio >= min_white_ratio
    
def mouse_callback(event, x, y, flags, param):
    """Callback для отслеживания положения мыши в окне"""
    global active_camera_id
    if event == cv2.EVENT_MOUSEMOVE:
        active_camera_id = param

def init_csv_log():
    """Инициализация CSV-файла для логов"""
    global csv_file_initialized
    if not csv_file_initialized:
        os.makedirs(os.path.dirname(CSV_LOG_PATH), exist_ok=True)
        if not os.path.exists(CSV_LOG_PATH):
            with open(CSV_LOG_PATH, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "camera_id", "class_id", "class_name", 
                    "confidence", "x1", "y1", "x2", "y2", "width", "height", "center_x", "center_y"
                ])
        csv_file_initialized = True

def log_detection_to_csv(camera_id, class_id, class_name, conf, box):
    """Запись детекции в CSV-файл"""
    init_csv_log()
    x1, y1, x2, y2 = map(int, box)
    timestamp = datetime.now().isoformat()
    with open(CSV_LOG_PATH, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            timestamp, camera_id, class_id, class_name,
            f"{conf:.3f}", x1, y1, x2, y2,
            x2-x1, y2-y1, (x1+x2)//2, (y1+y2)//2
        ])

def print_detection_log(camera_id, class_name, conf, box, frame_shape):
    """Печать детекции в консоль с троттлингом"""
    global last_log_time
    now = time.time() * 1000  # мс
    last = last_log_time.get(camera_id, 0)
    
    if not log_enabled or (now - last) < LOG_THROTTLE_MS:
        return
    
    last_log_time[camera_id] = now
    x1, y1, x2, y2 = map(int, box)
    h, w = frame_shape[:2]
    
    print(f"\n[{'='*60}]")
    print(f"📷 Камера {camera_id} | {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
    print(f"🎯 Объект: {class_name.upper()}")
    print(f"📊 Уверенность: {conf*100:.1f}%")
    print(f"📦 BBox: [{x1}, {y1}, {x2}, {y2}] | Размер: {x2-x1}×{y2-y1}")
    print(f"📍 Центр: ({(x1+x2)//2}, {(y1+y2)//2}) | Отн. позиция: ({x1/w:.2f}, {y1/h:.2f})")
    print(f"[{'='*60}]\n")

def draw_detection(frame, class_name, conf, box, color=(0, 255, 0)):
    """Отрисовка бокса и подписи на кадре"""
    x1, y1, x2, y2 = map(int, box)
    label = f"{class_name} {conf:.2f}"
    
    # Рисуем рамку
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    
    # Рисуем подпись на полупрозрачном фоне
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
    cv2.rectangle(frame, (x1, y1-25), (x1+tw, y1), color, -1)
    cv2.putText(frame, label, (x1, y1-5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
    
    # Рисуем точку в центре
    cx, cy = (x1+x2)//2, (y1+y2)//2
    cv2.circle(frame, (cx, cy), 4, (255, 0, 0), -1)
    
    return frame


class RTSPStream:
    """Класс для захвата и буферизации кадров из RTSP-потока"""
    
    def __init__(self, url, camera_id=0, buffer_size=1):
        self.url = url
        self.camera_id = camera_id
        self.buffer_size = buffer_size
        self.frames = []
        self.lock = threading.Lock()
        self.running = False
        self.cap = None
        self.thread = None
        self.reconnect_delay = 5
        
    def start(self):
        """Запуск потока захвата"""
        self.running = True
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
        print(f"[Камера {self.camera_id}] Запуск потока: {self.url}")
        return self
    
    def stop(self):
        """Остановка потока и освобождение ресурсов"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        if self.cap:
            self.cap.release()
        print(f"[Камера {self.camera_id}] Остановлен")
    
    def _capture_loop(self):
        """Основной цикл захвата кадров с авто-переподключением"""
        while self.running:
            try:
                self.cap = cv2.VideoCapture(self.url)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self.cap.set(cv2.CAP_PROP_FPS, 25)
                
                if not self.cap.isOpened():
                    raise ConnectionError(f"Не удалось открыть поток")
                
                print(f"[Камера {self.camera_id}] ✓ Подключено")
                
                while self.running:
                    ret, frame = self.cap.read()
                    if ret:
                        with self.lock:
                            if len(self.frames) >= self.buffer_size:
                                self.frames.pop(0)
                            self.frames.append(frame.copy())
                    else:
                        print(f"[Камера {self.camera_id}] ⚠ Потеря кадров, переподключение...")
                        break
                        
            except Exception as e:
                print(f"[Камера {self.camera_id}] ✗ Ошибка: {e}")
                if self.cap:
                    self.cap.release()
                time.sleep(self.reconnect_delay)
    
    def read(self):
        """Получение последнего кадра из буфера (оригинального размера)"""
        with self.lock:
            if self.frames:
                return self.frames[-1].copy()
        return None
    
    def get_latest_frame(self):
        """Алиас для read()"""
        return self.read()


def save_frame_manual(frame, camera_id, output_dir="recordings", prefix="manual"):
    """Сохранение кадра по запросу пользователя с визуальным подтверждением"""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = f"{output_dir}/{prefix}_cam{camera_id}_{timestamp}.jpg"
    cv2.imwrite(path, frame)
    print(f"[Камера {camera_id}] ✓ Сохранено: {path}")
    return path


def process_frame_with_yolo(frame, camera_id):
    """Запуск инференса YOLO и обработка результатов"""
    global model
    
    if not detection_enabled or model is None:
        return frame, []
    
    try:
        # Запуск инференса
        results = model.predict(
            source=frame,
            conf=CONFIDENCE_THRESHOLD,
            iou=IOU_THRESHOLD,
            verbose=False,
            device='cpu'  # Или '0' для CUDA, если доступно
        )
        
        detections = []
        processed_frame = frame.copy()
        
        for r in results:
            for box in r.boxes:
                cls_id = int(box.cls)
                conf = float(box.conf)
                class_name = model.names[cls_id]
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                
                # === ПРОВЕРКА ПО МАСКЕ ===
                if MASK_ENABLED:
                    mask = load_mask(camera_id, frame.shape)
                    if not is_detection_allowed(mask, (x1, y1, x2, y2)):
                        if log_enabled and log_enabled:  # Только если логирование включено
                            print(f"[Камера {camera_id}] ⛔ Отклонено по маске: {class_name} [{x1},{y1},{x2},{y2}]")
                        continue  # Пропускаем детекцию
                # =======================
                
                # Сохраняем данные детекции
                detection = {
                    'camera_id': camera_id,
                    'class_id': cls_id,
                    'class_name': class_name,
                    'confidence': conf,
                    'bbox': (x1, y1, x2, y2),
                    'timestamp': datetime.now()
                }
                detections.append(detection)
                
                # Отрисовка на кадре
                color = (0, 255, 0) if class_name == 'forklift' else (255, 100, 0)
                processed_frame = draw_detection(processed_frame, class_name, conf, (x1, y1, x2, y2), color)
                
                # Логирование
                if log_enabled:
                    print_detection_log(camera_id, class_name, conf, (x1, y1, x2, y2), frame.shape)
                    log_detection_to_csv(camera_id, cls_id, class_name, conf, (x1, y1, x2, y2))
        
        return processed_frame, detections
        
    except Exception as e:
        print(f"[Камера {camera_id}] ✗ Ошибка инференса: {e}")
        return frame, []


def display_streams(streams, reel_tracker=None, window_size=(640, 480)):
    """
    Отображение потоков с детекцией YOLO, обработкой клавиш и трекингом катушек.
    
    Args:
        streams: список объектов RTSPStream
        reel_tracker: объект ReelTracker для отслеживания перемещений катушек (опционально)
        window_size: размер окна отображения (width, height)
    """
    global active_camera_id, detection_enabled, log_enabled, SHOW_MASK_OVERLAY
    
    # Регистрация mouse callback для каждого окна
    for stream in streams:
        window_name = f'Camera {stream.camera_id}'
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(window_name, mouse_callback, stream.camera_id)
    
    print(f"\n🎮 Управление:")
    print(f"  [Q] — Выход")
    print(f"  [S] — Сохранить кадр с камеры под курсором")
    print(f"  [1], [2] — Сохранить кадр с конкретной камеры")
    print(f"  [D] — Вкл/выкл детекцию YOLO (сейчас: {'✅ ВКЛ' if detection_enabled else '❌ ВЫКЛ'})")
    print(f"  [L] — Вкл/выкл логирование в консоль (сейчас: {'✅ ВКЛ' if log_enabled else '❌ ВЫКЛ'})")
    print(f"  [M] — Вкл/выкл оверлей маски (сейчас: {'✅ ВКЛ' if SHOW_MASK_OVERLAY else '❌ ВЫКЛ'})")
    if reel_tracker is not None:
        print(f"  🎞️  Трекинг катушек: ✅ ВКЛ (переходы: {REEL_TRANSITION_LOG_PATH})")
    print(f"  Детекции сохраняются в: {CSV_LOG_PATH}\n")
    
    try:
        while True:
            # === ШАГ 1: Сбор всех детекций со всех камер для трекера ===
            all_detections_for_tracker = []  # Список всех детекций текущего цикла
            
            # === ШАГ 2: Обработка каждой камеры ===
            for stream in streams:
                frame = stream.read()
                if frame is None:
                    continue
                
                camera_id = stream.camera_id
                
                # Запуск детекции YOLO
                processed_frame, detections = process_frame_with_yolo(frame, camera_id)
                
                # Добавляем детекции в общий список для трекера (если трекер активен)
                if reel_tracker is not None and REEL_TRACKING_ENABLED:
                    all_detections_for_tracker.extend(detections)
                
                # === Отрисовка информации на кадре ===
                timestamp = datetime.now().strftime("%H:%M:%S")
                status = "🔍 DETECT" if detection_enabled else "📹 LIVE"
                indicator = " [← КУРСОР]" if camera_id == active_camera_id else ""
                
                # Статус-бар
                cv2.putText(processed_frame, f"Cam {camera_id} | {timestamp} | {status}{indicator}", 
                           (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                
                # Счётчик детекций
                if detections:
                    counts = {}
                    for d in detections:
                        counts[d['class_name']] = counts.get(d['class_name'], 0) + 1
                    info_text = " | ".join([f"{k}: {v}" for k, v in counts.items()])
                    cv2.putText(processed_frame, info_text, (10, 60), 
                               cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
                
                # === ОТЛАДКА: показать маску поверх кадра ===
                if SHOW_MASK_OVERLAY and MASK_ENABLED and camera_id in MASK_PATHS:
                    mask = load_mask(camera_id, frame.shape)
                    if mask is not None:
                        mask_color = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
                        mask_color[mask < 127] = [0, 0, 255]      # Чёрный → красный (запрет)
                        mask_color[mask >= 127] = [0, 255, 0]     # Белый → зелёный (разрешено)
                        processed_frame = cv2.addWeighted(processed_frame, 0.7, mask_color, 0.3, 0)
                        cv2.putText(processed_frame, "MASK OVERLAY [M]", (10, 90), 
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
                
                # Отображение кадра
                display_frame = cv2.resize(processed_frame, window_size)
                cv2.imshow(f'Camera {camera_id}', display_frame)
            
            # === ШАГ 3: Обновление трекера катушек (после обработки всех камер) ===
            if reel_tracker is not None and REEL_TRACKING_ENABLED:
                # Группируем детекции по ID камеры
                detections_by_cam = {}
                for det in all_detections_for_tracker:
                    cid = det['camera_id']
                    if cid not in detections_by_cam:
                        detections_by_cam[cid] = []
                    detections_by_cam[cid].append(det)
                
                # Обновляем трекер для каждой камеры, где есть детекции
                for cid, dets in detections_by_cam.items():
                    reel_tracker.update(camera_id=cid, all_detections=dets)
            
            # === ШАГ 4: Обработка нажатий клавиш ===
            key = cv2.waitKey(1) & 0xFF
            
            if key == ord('q'):  # Выход
                break
            
            elif key == ord('d') or key == ord('D'):  # Toggle detection
                detection_enabled = not detection_enabled
                status = "✅ ВКЛ" if detection_enabled else "❌ ВЫКЛ"
                print(f"\n🔍 Детекция YOLO: {status}\n")
            
            elif key == ord('l') or key == ord('L'):  # Toggle logging
                log_enabled = not log_enabled
                status = "✅ ВКЛ" if log_enabled else "❌ ВЫКЛ"
                print(f"\n📝 Логирование: {status}\n")
            
            elif key == ord('m') or key == ord('M'):  # Toggle mask overlay
                SHOW_MASK_OVERLAY = not SHOW_MASK_OVERLAY
                status = "✅ ВКЛ" if SHOW_MASK_OVERLAY else "❌ ВЫКЛ"
                print(f"\n🎭 Оверлей маски: {status}\n")
            
            elif key == ord('s') or key == ord('S'):  # Save frame under cursor
                if active_camera_id is not None:
                    stream = next((s for s in streams if s.camera_id == active_camera_id), None)
                    if stream:
                        original_frame = stream.get_latest_frame()
                        if original_frame is not None:
                            save_frame_manual(original_frame, active_camera_id)
            
            elif key in [ord(str(i)) for i in range(len(streams))]:  # Save specific camera
                cam_idx = int(chr(key))
                if cam_idx < len(streams):
                    stream = streams[cam_idx]
                    original_frame = stream.get_latest_frame()
                    if original_frame is not None:
                        save_frame_manual(original_frame, cam_idx)
                        
    except KeyboardInterrupt:
        print("\n🛑 Остановка по запросу пользователя...")
    finally:
        # Очистка окон
        for stream in streams:
            cv2.destroyWindow(f'Camera {stream.camera_id}')

class ReelTracker:
    """
    Трекер перемещений катушек между зонами (улица ↔ помещение).
    Фильтрует шумы, определяет режим перемещения (погрузчик/человек),
    логирует только подтверждённые переходы.
    """
    
    def __init__(self, indoor_cam_id=0, outdoor_cam_id=1):
        self.indoor_cam_id = indoor_cam_id
        self.outdoor_cam_id = outdoor_cam_id
        self.tracks = {}  # {reel_id: track_data}
        self.next_reel_id = 0
        self.transition_events = []
        self.lock = threading.Lock()
        
    def _get_zone(self, camera_id):
        return "indoor" if camera_id == self.indoor_cam_id else "outdoor"
    
    def _boxes_nearby(self, box1, box2, threshold=REEL_NEARBY_THRESHOLD_PX):
        """Проверка расстояния между центрами двух боксов"""
        cx1, cy1 = (box1[0]+box1[2])//2, (box1[1]+box1[3])//2
        cx2, cy2 = (box2[0]+box2[2])//2, (box2[1]+box2[3])//2
        return ((cx1-cx2)**2 + (cy1-cy2)**2)**0.5 < threshold
    
    def _determine_transport_mode(self, reel_box, all_detections):
        """
        Определение режима перемещения катушки.
        Returns: 'forklift', 'human', 'human_nearby', 'unknown'
        """
        has_forklift = False
        has_human = False
        
        for det in all_detections:
            if det['class_name'] == 'forklift' and self._boxes_nearby(reel_box, det['bbox']):
                has_forklift = True
            elif det['class_name'] == 'human' and self._boxes_nearby(reel_box, det['bbox']):
                has_human = True
        
        if has_forklift and has_human:
            return 'human_nearby'  # Человек просто рядом, несёт погрузчик
        elif has_forklift:
            return 'forklift'
        elif has_human:
            return 'human'
        return 'unknown'
    
    def update(self, camera_id, all_detections, timestamp=None):
        """Обновление трекера детекциями с одной камеры"""
        if timestamp is None:
            timestamp = time.time()
        
        with self.lock:
            reel_dets = [d for d in all_detections if d['class_name'] == REEL_CLASS_NAME]
            
            for reel_det in reel_dets:
                reel_box = reel_det['bbox']
                transport_mode = self._determine_transport_mode(reel_box, all_detections)
                zone = self._get_zone(camera_id)
                
                # Простая ассоциация: ближайший активный трек в той же зоне
                assigned_track = None
                min_dist = float('inf')
                
                for track_id, track in self.tracks.items():
                    if track['last_zone'] != zone:
                        continue
                    last_box = track['last_bbox']
                    if last_box:
                        cx1, cy1 = (reel_box[0]+reel_box[2])//2, (reel_box[1]+reel_box[3])//2
                        cx2, cy2 = (last_box[0]+last_box[2])//2, (last_box[1]+last_box[3])//2
                        dist = ((cx1-cx2)**2 + (cy1-cy2)**2)**0.5
                        if dist < min_dist and dist < 200:
                            min_dist = dist
                            assigned_track = track_id
                
                if assigned_track is None:
                    # Новый трек
                    track_id = self.next_reel_id
                    self.next_reel_id += 1
                    self.tracks[track_id] = {
                        'id': track_id,
                        'last_zone': zone,
                        'last_bbox': reel_box,
                        'last_update': timestamp,
                        'confirmed_zone': zone,
                        'pending_transition': None
                    }
                else:
                    track = self.tracks[assigned_track]
                    track['last_bbox'] = reel_box
                    track['last_update'] = timestamp
                    
                    # Проверка перехода
                    if zone != track['confirmed_zone']:
                        self._handle_pending_transition(assigned_track, zone, transport_mode, timestamp)
                    else:
                        track['pending_transition'] = None  # Сброс, если вернулись
            
            # Очистка старых треков
            self._cleanup_tracks(timestamp)
    
    def _handle_pending_transition(self, track_id, new_zone, transport_mode, timestamp):
        """Логика подтверждения перехода между зонами"""
        track = self.tracks[track_id]
        pending = track.get('pending_transition')
        
        # Инициализация или обновление pending
        if pending is None or pending['target_zone'] != new_zone:
            track['pending_transition'] = {
                'target_zone': new_zone,
                'start_time': timestamp,
                'count': 1,
                'modes': [(transport_mode, timestamp)]
            }
        else:
            pending['count'] += 1
            pending['modes'].append((transport_mode, timestamp))
        
        # Проверка условий подтверждения
        pending = track['pending_transition']
        elapsed = timestamp - pending['start_time']
        
        if elapsed >= TRANSITION_CONFIRM_WINDOW_SEC and pending['count'] >= TRANSITION_MIN_DETECTIONS:
            # Определение преобладающего режима
            modes = [m for m, _ in pending['modes']]
            dominant_mode = max(set(modes), key=modes.count)
            
            direction = "ENTERED" if new_zone == "indoor" else "EXITED"
            event = {
                'timestamp': datetime.now().isoformat(),
                'reel_id': track_id,
                'direction': direction,
                'from_zone': track['confirmed_zone'],
                'to_zone': new_zone,
                'transport_mode': dominant_mode,
                'camera_id': self.indoor_cam_id if new_zone == "indoor" else self.outdoor_cam_id
            }
            
            self.transition_events.append(event)
            self._log_transition(event)
            
            # Обновление трека
            track['confirmed_zone'] = new_zone
            track['pending_transition'] = None
    
    def _log_transition(self, event):
        """Вывод события в консоль и CSV"""
        # Консоль
        mode_emoji = {'forklift': '🚜', 'human': '🚶', 'human_nearby': '🚶+🚜', 'unknown': '❓'}
        dir_emoji = {'ENTERED': '➡️🏢', 'EXITED': '🏢➡️'}
        
        print(f"\n[{'='*70}]")
        print(f"🎞️  ПЕРЕХОД КАТУШКИ #{event['reel_id']}")
        print(f"{dir_emoji.get(event['direction'], '')} {event['direction']} в {event['to_zone'].upper()}")
        print(f"🕐 Время: {event['timestamp']}")
        print(f"🔧 Режим: {event['transport_mode']} {mode_emoji.get(event['transport_mode'], '')}")
        print(f"📷 Камера подтверждения: {event['camera_id']}")
        print(f"[{'='*70}]\n")
        
        # CSV
        os.makedirs(os.path.dirname(REEL_TRANSITION_LOG_PATH), exist_ok=True)
        file_exists = os.path.exists(REEL_TRANSITION_LOG_PATH)
        
        with open(REEL_TRANSITION_LOG_PATH, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(['timestamp', 'reel_id', 'direction', 'from_zone', 'to_zone', 'transport_mode', 'camera_id'])
            writer.writerow([
                event['timestamp'], event['reel_id'], event['direction'],
                event['from_zone'], event['to_zone'], event['transport_mode'], event['camera_id']
            ])
    
    def _cleanup_tracks(self, current_time):
        """Удаление неактивных треков"""
        to_remove = [tid for tid, t in self.tracks.items() 
                     if current_time - t['last_update'] > MAX_REEL_TRACK_AGE_SEC]
        for tid in to_remove:
            del self.tracks[tid]
    
    def get_events(self):
        """Получение накопленных событий"""
        with self.lock:
            events = self.transition_events.copy()
            self.transition_events.clear()
            return events
            
            

def main():
    global model
    
    # === Загрузка модели YOLO ===
    if os.path.exists(MODEL_PATH):
        print(f"🤖 Загрузка модели: {MODEL_PATH}")
        from ultralytics import YOLO
        model = YOLO(MODEL_PATH)
        print(f"✅ Модель загружена | Классы: {model.names}")
    else:
        print(f"⚠️  Модель не найдена: {MODEL_PATH}")
        return
    
    # === Инициализация трекера катушек (если включено) ===
    reel_tracker = None
    if REEL_TRACKING_ENABLED:
        reel_tracker = ReelTracker(indoor_cam_id=0, outdoor_cam_id=1)
        print(f"🎞️  Трекинг катушек активирован")
    
    # Инициализация потоков
    streams = [RTSPStream(url, camera_id=i).start() for i, url in enumerate(RTSP_URLS)]
    
    try:
        # === Передаём reel_tracker в display_streams ===
        display_streams(streams, reel_tracker=reel_tracker)
    finally:
        for stream in streams:
            stream.stop()
        cv2.destroyAllWindows()
        print("👋 Все ресурсы освобождены")



if __name__ == "__main__":
    main()