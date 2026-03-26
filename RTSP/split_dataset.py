import os
import shutil
import random
from pathlib import Path

# Настройки путей
BASE_DIR = Path.cwd()  # Текущая папка (RTSP)
SOURCE_DIR = BASE_DIR / "recordings"
DATASET_DIR = BASE_DIR / "dataset"

# Коэффициент разбиения (80% train, 20% val)
TRAIN_RATIO = 0.8

def split_dataset():
    print(f"Источник: {SOURCE_DIR}")
    print(f"Назначение: {DATASET_DIR}")

    # 1. Очистка и создание структуры папок
    if DATASET_DIR.exists():
        shutil.rmtree(DATASET_DIR)
    
    dirs = [
        DATASET_DIR / "train" / "images",
        DATASET_DIR / "train" / "labels",
        DATASET_DIR / "val" / "images",
        DATASET_DIR / "val" / "labels"
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # 2. Сбор пар файлов (jpg + txt)
    images = list(SOURCE_DIR.glob("*.jpg"))
    pairs = []
    
    for img_path in images:
        txt_path = img_path.with_suffix(".txt")
        if txt_path.exists():
            pairs.append((img_path, txt_path))
        else:
            print(f"Внимание: Для {img_path.name} не найдено разметки .txt")

    print(f"Найдено пар файлов: {len(pairs)}")

    if len(pairs) == 0:
        print("Ошибка: Не найдено пар изображений и разметки!")
        return

    # 3. Перемешивание
    random.shuffle(pairs)

    # 4. Разбиение
    split_idx = int(len(pairs) * TRAIN_RATIO)
    train_pairs = pairs[:split_idx]
    val_pairs = pairs[split_idx:]

    print(f"Обучающая выборка: {len(train_pairs)}")
    print(f"Валидационная выборка: {len(val_pairs)}")

    # 5. Копирование файлов
    def copy_pairs(pairs_list, set_name):
        for img_path, txt_path in pairs_list:
            shutil.copy(img_path, DATASET_DIR / set_name / "images" / img_path.name)
            shutil.copy(txt_path, DATASET_DIR / set_name / "labels" / txt_path.name)

    copy_pairs(train_pairs, "train")
    copy_pairs(val_pairs, "val")

    # 6. Создание data.yaml
    yaml_content = f"""path: {DATASET_DIR.as_posix()}
train: train/images
val: val/images

nc: 3
names:
  0: forklift
  1: cable_reel
  2: human
"""
    yaml_path = DATASET_DIR / "data.yaml"
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(yaml_content)
    
    print(f"Конфигурация сохранена в: {yaml_path}")
    print("Разбиение завершено успешно!")

if __name__ == "__main__":
    split_dataset()