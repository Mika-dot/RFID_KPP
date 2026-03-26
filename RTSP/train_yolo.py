from ultralytics import YOLO
import os

# Путь к файлу конфигурации, созданному первым скриптом
DATASET_CONFIG = "dataset/data.yaml"

def train_model():
    # Проверка наличия файла конфигурации
    if not os.path.exists(DATASET_CONFIG):
        print(f"Ошибка: Файл {DATASET_CONFIG} не найден.")
        print("Сначала запустите split_dataset.py")
        return

    print("Загрузка модели YOLOv8n (легкая версия для старта)...")
    # Используем предобученную модель для ускорения обучения
    model = YOLO("yolov8n.pt") 

    print("Начало обучения...")
    # Параметры обучения:
    # epochs: количество эпох (проходов по датасету)
    # imgsz: размер изображения (640 стандарт)
    # batch: размер пачки (зависит от видеокарты, -1 авто)
    # patience: остановка, если нет улучшений N эпох
    results = model.train(
        data=DATASET_CONFIG,
        epochs=50,          # Можно увеличить до 100, если данных мало
        imgsz=640,
        batch=-1,
        patience=10,
        name="rfid_forklift_reel", # Имя папки с результатами
        verbose=True
    )

    print("Обучение завершено!")
    print(f"Лучшие веса модели сохранены в: runs/detect/rfid_forklift_reel/weights/best.pt")

if __name__ == "__main__":
    train_model()