import cv2
import threading
import time
import os
from datetime import datetime

# Получение учетных данных из переменных окружения (рекомендуется)
USERNAME = os.getenv("CAMERA_USER", "Akim")
PASSWORD = os.getenv("CAMERA_PASS", "MylenE12")

RTSP_URLS = [
    f"rtsp://{USERNAME}:{PASSWORD}@10.192.2.11:554/Streaming/Channels/101",
    f"rtsp://{USERNAME}:{PASSWORD}@10.192.2.12:554/Streaming/Channels/101"
]

# Глобальная переменная для отслеживания камеры под курсором
active_camera_id = None

def mouse_callback(event, x, y, flags, param):
    """Callback для отслеживания положения мыши в окне"""
    global active_camera_id
    if event == cv2.EVENT_MOUSEMOVE:
        active_camera_id = param  # param = camera_id при регистрации

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


def display_streams(streams, window_size=(640, 480)):
    """Отображение потоков с обработкой нажатий для сохранения кадров"""
    global active_camera_id
    
    # Регистрация mouse callback для каждого окна
    for stream in streams:
        window_name = f'Camera {stream.camera_id}'
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(window_name, mouse_callback, stream.camera_id)
    
    try:
        while True:
            frames = [s.read() for s in streams]
            
            if all(f is None for f in frames):
                time.sleep(0.1)
                continue
            
            for i, (frame, stream) in enumerate(zip(frames, streams)):
                if frame is not None:
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    
                    # Визуальная индикация активной камеры (под курсором)
                    indicator = " [← КУРСОР]" if stream.camera_id == active_camera_id else ""
                    cv2.putText(frame, f"Cam {stream.camera_id} | {timestamp}{indicator}", 
                               (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    
                    display_frame = cv2.resize(frame, window_size)
                    cv2.imshow(f'Camera {stream.camera_id}', display_frame)
            
            # Обработка клавиш
            key = cv2.waitKey(1) & 0xFF
            
            # Выход по 'q'
            if key == ord('q'):
                break
            
            # Сохранение по 'S' для камеры под курсором
            elif key == ord('s') or key == ord('S'):
                if active_camera_id is not None:
                    stream = next((s for s in streams if s.camera_id == active_camera_id), None)
                    if stream:
                        original_frame = stream.get_latest_frame()
                        if original_frame is not None:
                            save_frame_manual(original_frame, active_camera_id)
                            # Визуальное подтверждение на всех окнах
                            for s in streams:
                                cv2.putText(
                                    cv2.resize(original_frame if s.camera_id == active_camera_id else stream.get_latest_frame(), window_size),
                                    "SAVED!", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2
                                )
                else:
                    print("⚠ Наведите курсор на окно камеры перед нажатием S")
            
            # Сохранение по цифровой клавише (1, 2, 3...) для конкретной камеры
            elif key in [ord(str(i)) for i in range(len(streams))]:
                cam_idx = int(chr(key))
                if cam_idx < len(streams):
                    stream = streams[cam_idx]
                    original_frame = stream.get_latest_frame()
                    if original_frame is not None:
                        save_frame_manual(original_frame, cam_idx)
                        
    except KeyboardInterrupt:
        print("\nОстановка по запросу пользователя...")
    finally:
        for stream in streams:
            cv2.destroyWindow(f'Camera {stream.camera_id}')


def save_frame_callback(frame, camera_id, output_dir="recordings"):
    """Пример колбэка для автоматического сохранения или передачи в модель ИИ"""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"{output_dir}/cam{camera_id}_{timestamp}.jpg"
    cv2.imwrite(path, frame)
    # results = model(frame)  # например, YOLO через ultralytics или ONNX Runtime


def main():
    streams = [RTSPStream(url, camera_id=i).start() for i, url in enumerate(RTSP_URLS)]
    
    try:
        display_streams(streams)
    finally:
        for stream in streams:
            stream.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()