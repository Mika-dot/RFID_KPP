#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RFID → MS SQL Server
Конфигурация ТОЛЬКО через переменные окружения.
"""
import os
import sys
import time
import ctypes
import logging
from datetime import datetime
from typing import Optional, Dict, List

class TagDeduplicator:
    """Фильтр повторных чтений одной и той же метки"""
    def __init__(self, ttl_seconds=5):
        self.seen_tags = {}  # {epc: last_read_timestamp}
        self.ttl = ttl_seconds
    
    def should_save(self, epc: str) -> bool:
        """Возвращает True, если метку стоит записать"""
        import time
        now = time.time()
        
        if epc in self.seen_tags:
            if now - self.seen_tags[epc] < self.ttl:
                return False  # Слишком рано, пропускаем
        
        self.seen_tags[epc] = now
        return True
    
    def cleanup(self):
        """Очистка старых записей (раз в минуту)"""
        import time
        now = time.time()
        self.seen_tags = {k: v for k, v in self.seen_tags.items() if now - v < self.ttl * 2}
        
# 🔌 Поддержка .env файла (опционально)
try:
    from dotenv import load_dotenv
    load_dotenv()  # Загружает переменные из .env, если файл есть
except ImportError:
    pass  # Если dotenv нет — работаем только с системными переменными

# === Настройка логирования ===
LOG_LEVEL = os.getenv("RFID_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

# === Обязательные переменные окружения ===
REQUIRED_ENV = [
    "RFID_DLL_PATH",
    "RFID_READER_IP", 
    "RFID_READER_PORT",
    "RFID_DB_CONNECTION"
]

def check_env() -> bool:
    """Проверка наличия всех обязательных переменных"""
    missing = [var for var in REQUIRED_ENV if not os.getenv(var)]
    if missing:
        logger.error("❌ Отсутствуют обязательные переменные окружения:")
        for var in missing:
            logger.error(f"   • {var}")
        logger.error("\nПример запуска:")
        logger.error("   set RFID_DLL_PATH=C:\\path\\to\\UHFAPI.dll")
        logger.error("   set RFID_READER_IP=172.31.128.170")
        logger.error("   set RFID_READER_PORT=8888")
        logger.error("   set RFID_DB_CONNECTION=Server=...;Database=...;...")
        return False
    return True

# === Глобальные настройки из env ===
DLL_PATH = os.getenv("RFID_DLL_PATH")
READER_IP = os.getenv("RFID_READER_IP")
READER_PORT = int(os.getenv("RFID_READER_PORT"))
DB_CONN_STR = os.getenv("RFID_DB_CONNECTION")
CONSOLE_OUTPUT = os.getenv("RFID_CONSOLE_OUTPUT", "1") == "1"
BATCH_SIZE = int(os.getenv("RFID_BATCH_SIZE", "1"))  # 1 = запись по одной записи

# === Парсинг буфера (без изменений) ===
def parse_buf(buf, length: int) -> Optional[Dict]:
    """Парсит буфер ТОЧНО как C# uhfGetReceived()"""
    if length < 8:
        return None
    try:
        d = [b & 0xFF for b in buf[:length]]
        uii_len = d[0]
        if uii_len < 3 or uii_len + 1 >= length:
            return None
        
        tid_len = d[uii_len + 1]
        tid_start = uii_len + 2
        if tid_start + tid_len > length:
            tid_len = length - tid_start
        
        rssi_idx = tid_start + tid_len
        if rssi_idx + 3 > length:
            return None
        
        ant_idx = rssi_idx + 2
        
        # EPC: пропускаем 3 байта, берём (uii_len*2 - 4) hex-символов
        epc_start = 3
        epc_hex_len = uii_len * 2 - 4
        epc_end = epc_start + (epc_hex_len // 2)
        if epc_end > len(d):
            return None
        epc = "".join(f"{b:02X}" for b in d[epc_start:epc_end])
        
        # TID
        tid = "".join(f"{b:02X}" for b in d[tid_start:tid_start+tid_len]) if tid_len > 0 else ""
        
        # RSSI: big-endian uint16, формула (val - 65535) / 10.0
        rssi_raw = (d[rssi_idx] << 8) | d[rssi_idx + 1]
        rssi = (rssi_raw - 65535) / 10.0
        
        ant = d[ant_idx]
        return {"epc": epc, "tid": tid, "rssi": rssi, "ant": ant}
    except Exception as e:
        logger.debug(f"Ошибка парсинга буфера: {e}")
        return None

# === Работа с БД ===
def test_db_connection() -> bool:
    """Проверка подключения к БД"""
    try:
        import pyodbc
        with pyodbc.connect(DB_CONN_STR, timeout=5) as conn:
            conn.cursor().execute("SELECT 1")
        logger.info("✅ БД: подключение успешно")
        return True
    except ImportError:
        logger.critical("❌ Не установлен pyodbc. Выполните: pip install pyodbc")
        return False
    except Exception as e:
        logger.error(f"❌ БД: ошибка подключения: {e}")
        return False

def insert_tags(tags: List[Dict]) -> int:
    """
    Вставка списка записей в БД.
    Возвращает количество успешно записанных записей.
    """
    if not tags:
        return 0
    
    try:
        import pyodbc
        conn = pyodbc.connect(DB_CONN_STR, timeout=5)
        cursor = conn.cursor()
        
        # Пакетная вставка
        values = [
            (
                datetime.now(),           # RecordTime
                tag["tag_time"],          # TagTime
                int(tag["data"]["ant"]),  # Antenna
                round(tag["data"]["rssi"], 1),  # RSSI
                tag["data"]["epc"],       # EPC
                tag["data"]["tid"] if tag["data"]["tid"] else None  # TID
            )
            for tag in tags
        ]
        
        cursor.executemany(
            """
            INSERT INTO dbo.RFID_Tags (RecordTime, TagTime, Antenna, RSSI, EPC, TID)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            values
        )
        conn.commit()
        cursor.close()
        conn.close()
        logger.debug(f"✅ БД: записано {len(values)} записей")
        return len(values)
        
    except ImportError:
        logger.critical("❌ Не установлен pyodbc")
        return 0
    except Exception as e:
        logger.error(f"❌ БД: ошибка записи: {e}")
        return 0

# === Основная логика ===
def main() -> int:
    # 1. Проверка переменных окружения
    if not check_env():
        return 1
    
    logger.info(f"🚀 Запуск: {READER_IP}:{READER_PORT}")
    logger.debug(f"📦 DLL: {DLL_PATH}")
    logger.debug(f"🗄️  БД: {DB_CONN_STR[:50]}...")  # Логирование без пароля
    
    # 2. Проверка БД
    if not test_db_connection():
        logger.error("❌ Запуск невозможен без подключения к БД")
        return 1
    
    # 3. Проверка DLL
    if not os.path.exists(DLL_PATH):
        logger.error(f"❌ DLL не найдена: {DLL_PATH}")
        return 1
    
    # 4. Загрузка DLL
    try:
        lib = ctypes.CDLL(DLL_PATH)
        logger.info("✅ DLL загружена")
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки DLL: {e}")
        logger.error("💡 Убедитесь, что разрядность Python и DLL совпадает (32/64 бит)")
        return 1
    
    # 5. Прототипы функций DLL
    try:
        lib.TCPConnect.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        lib.TCPConnect.restype = ctypes.c_int
        lib.TCPDisconnect.argtypes = []
        lib.TCPDisconnect.restype = None
        lib.UHFInventory.argtypes = []
        lib.UHFInventory.restype = ctypes.c_int
        lib.UHFStopGet.argtypes = []
        lib.UHFStopGet.restype = ctypes.c_int
        lib.UHF_GetReceived_EX.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ubyte * 512)
        ]
        lib.UHF_GetReceived_EX.restype = ctypes.c_int
    except Exception as e:
        logger.error(f"❌ Ошибка настройки прототипов DLL: {e}")
        return 1
    
    # 6. Подключение к считывателю
    logger.info(f"🔌 Подключение к {READER_IP}:{READER_PORT}...")
    if lib.TCPConnect(READER_IP.encode(), READER_PORT) != 0:
        logger.error("❌ Не удалось подключиться к считывателю")
        return 1
    logger.info("✅ Подключено к считывателю")
    
    # 7. Заголовок консоли (если включено)
    if CONSOLE_OUTPUT:
        print("\n" + "="*95)
        print(f"{'ВРЕМЯ':<10} {'АНТ':<4} {'RSSI':<8} {'EPC':<42} {'TID':<24} {'СТАТУС':<8}")
        print("="*95)
        sys.stdout.flush()
    
    # 8. Старт инвентаризации
    lib.UHFInventory()
    logger.info("📡 Инвентаризация запущена")
    
    # 9. Инициализация фильтра дублей и переменных цикла
    # 🔥 Ключ: (EPC, антенна) — чтобы одна метка на разных антеннах считалась отдельно
    seen_tags: Dict[tuple, float] = {}
    DEDUP_TTL = float(os.getenv("RFID_DEDUP_TTL", "5"))  # секунды (из env или 5 по умолчанию)
    
    batch: List[Dict] = []
    last_flush = time.time()
    last_cleanup = time.time()
    
    try:
        while True:
            uLen = ctypes.c_int(0)
            buf = (ctypes.c_ubyte * 512)()
            res = lib.UHF_GetReceived_EX(ctypes.byref(uLen), ctypes.byref(buf))
            
            if res == 0 and uLen.value > 0:
                tag_data = parse_buf(buf, uLen.value)
                if tag_data:
                    tag_time = time.strftime("%H:%M:%S")
                    epc = tag_data["epc"]
                    ant = tag_data["ant"]
                    
                    # === 🔥 ФИЛЬТР ДУБЛЕЙ ===
                    tag_key = (epc, ant)  # Уникальный ключ: метка + антенна
                    now = time.time()
                    
                    if tag_key in seen_tags:
                        if now - seen_tags[tag_key] < DEDUP_TTL:
                            # Дубль — пропускаем запись в БД
                            if CONSOLE_OUTPUT:
                                print(f"{tag_time:<10} {ant:<4} {tag_data['rssi']:<8.1f} "
                                      f"{epc:<42} {tag_data['tid']:<24} {'⊘ дубль':<8}")
                                sys.stdout.flush()
                            continue  # ⏭️ Пропускаем итерацию
                        else:
                            # TTL истёк — обновляем время и разрешаем запись
                            seen_tags[tag_key] = now
                    else:
                        # Новая метка — запоминаем время первого чтения
                        seen_tags[tag_key] = now
                    
                    # === Запись в пакет ===
                    batch.append({"data": tag_data, "tag_time": tag_time})
                    
                    # Консольный вывод (только для новых/обновлённых записей)
                    if CONSOLE_OUTPUT:
                        print(f"{tag_time:<10} {ant:<4} {tag_data['rssi']:<8.1f} "
                              f"{epc:<42} {tag_data['tid']:<24} {'✓ запис':<8}")
                        sys.stdout.flush()
                    
                    # Пакетная запись в БД
                    if len(batch) >= BATCH_SIZE:
                        written = insert_tags(batch)
                        batch.clear()
                        if written > 0:
                            logger.debug(f"📤 Отправлено {written} записей в БД")
            
            # === Принудительная запись пакета раз в 1 секунду (если BATCH_SIZE > 1) ===
            if BATCH_SIZE > 1 and batch and (time.time() - last_flush) >= 1.0:
                written = insert_tags(batch)
                batch.clear()
                last_flush = time.time()
                if written > 0:
                    logger.debug(f"📤 [таймаут] Отправлено {written} записей в БД")
            
            # === Очистка кэша дублей раз в 60 секунд ===
            if time.time() - last_cleanup >= 60:
                # Удаляем записи старше 2×TTL
                cutoff = time.time() - (DEDUP_TTL * 2)
                seen_tags = {k: v for k, v in seen_tags.items() if v > cutoff}
                last_cleanup = time.time()
                logger.debug(f"🧹 Очистка кэша дублей: осталось {len(seen_tags)} записей")
            
            time.sleep(0.01)  # Снижаем нагрузку на CPU
            
    except KeyboardInterrupt:
        logger.info("⏹️  Остановка по запросу пользователя (Ctrl+C)")
    except Exception as e:
        logger.exception(f"💥 Неожиданная ошибка: {e}")
    finally:
        # Очистка остатков пакета
        if batch:
            written = insert_tags(batch)
            logger.info(f"📤 Завершение: записано ещё {written} записей")
        
        # Остановка считывателя
        lib.UHFStopGet()
        lib.TCPDisconnect()
        logger.info("🔌 Отключено от считывателя")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())