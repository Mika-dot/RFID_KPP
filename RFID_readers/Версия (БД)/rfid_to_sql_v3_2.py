#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RFID -> MS SQL Server (improved, stable insert)

Исправления относительно v3.1:
- убран проблемный fast_executemany/executemany, который давал HY000 right truncation
- запись идет по одной строке через cursor.execute(), как в исходном рабочем варианте
- commit один раз на пакет
- консольный статус "✓ запис" ставится только после успешной записи в БД
"""
import os
import sys
import time
import ctypes
import logging
import json
import atexit
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

LOG_LEVEL = os.getenv("RFID_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
    force=True,
)
logger = logging.getLogger(__name__)

REQUIRED_ENV = [
    "RFID_DLL_PATH",
    "RFID_READER_IP",
    "RFID_READER_PORT",
    "RFID_DB_CONNECTION",
]


def env_int(name: str, default: Optional[int] = None, required: bool = False) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        if required:
            raise ValueError(f"Не задана переменная {name}")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть int, сейчас: {raw!r}") from exc


def env_float(name: str, default: Optional[float] = None, required: bool = False) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        if required:
            raise ValueError(f"Не задана переменная {name}")
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть float, сейчас: {raw!r}") from exc


def check_env() -> bool:
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        logger.error("❌ Отсутствуют обязательные переменные окружения:")
        for name in missing:
            logger.error("   • %s", name)
        return False
    try:
        env_int("RFID_READER_PORT", required=True)
        env_int("RFID_BATCH_SIZE", default=1)
        env_float("RFID_BATCH_FLUSH_SEC", default=0.5)
        env_float("RFID_DEDUP_TTL", default=30.0)
        env_float("RFID_RECONNECT_DELAY_SEC", default=5.0)
    except ValueError as exc:
        logger.error("❌ Ошибка конфигурации: %s", exc)
        return False
    return True


class Config:
    DLL_PATH: str = ""
    READER_IP: str = ""
    READER_PORT: int = 0
    DB_CONN_STR: str = ""
    CONSOLE_OUTPUT: bool = True
    BATCH_SIZE: int = 1
    BATCH_FLUSH_INTERVAL: float = 0.5
    DEDUP_ENABLED: bool = False
    DEDUP_TTL: float = 30.0
    DEDUP_CACHE_FILE: str = "rfid_dedup_cache.json"
    FALLBACK_LOG: str = "rfid_fallback.log"
    RECONNECT_DELAY_SEC: float = 5.0
    READ_SLEEP_SEC: float = 0.01
    IDLE_SLEEP_SEC: float = 0.05
    ERROR_SLEEP_SEC: float = 1.0

    @classmethod
    def load(cls) -> "Config":
        cls.DLL_PATH = os.getenv("RFID_DLL_PATH", "")
        cls.READER_IP = os.getenv("RFID_READER_IP", "")
        cls.READER_PORT = env_int("RFID_READER_PORT", required=True)
        cls.DB_CONN_STR = os.getenv("RFID_DB_CONNECTION", "")
        cls.CONSOLE_OUTPUT = os.getenv("RFID_CONSOLE_OUTPUT", "1") == "1"
        cls.BATCH_SIZE = max(1, env_int("RFID_BATCH_SIZE", default=1))
        cls.BATCH_FLUSH_INTERVAL = max(0.1, env_float("RFID_BATCH_FLUSH_SEC", default=0.5))
        cls.DEDUP_ENABLED = os.getenv("RFID_DEDUP_ENABLED", "0") == "1"
        cls.DEDUP_TTL = max(0.5, env_float("RFID_DEDUP_TTL", default=30.0))
        cls.DEDUP_CACHE_FILE = os.getenv("RFID_DEDUP_CACHE", "rfid_dedup_cache.json")
        cls.FALLBACK_LOG = os.getenv("RFID_FALLBACK_LOG", "rfid_fallback.log")
        cls.RECONNECT_DELAY_SEC = max(1.0, env_float("RFID_RECONNECT_DELAY_SEC", default=5.0))
        cls.READ_SLEEP_SEC = max(0.001, env_float("RFID_READ_SLEEP_SEC", default=0.01))
        cls.IDLE_SLEEP_SEC = max(0.001, env_float("RFID_IDLE_SLEEP_SEC", default=0.05))
        cls.ERROR_SLEEP_SEC = max(0.1, env_float("RFID_ERROR_SLEEP_SEC", default=1.0))
        return cls


class TagDeduplicator:
    def __init__(self, ttl_seconds: float = 30.0, cache_file: Optional[str] = None):
        self.seen_tags: Dict[tuple, float] = {}
        self.ttl = ttl_seconds
        self.cache_file = cache_file
        self._load_cache()

    @staticmethod
    def _serialize_key(key: tuple) -> str:
        epc, ant = key
        return f"{epc}|{ant}"

    @staticmethod
    def _deserialize_key(key: str) -> tuple:
        epc, ant = key.rsplit("|", 1)
        return (epc, int(ant))

    def _load_cache(self):
        if not self.cache_file or not os.path.exists(self.cache_file):
            return
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            now = time.time()
            restored = {}
            for key_str, ts in loaded.items():
                if now - ts < self.ttl * 2:
                    restored[self._deserialize_key(key_str)] = ts
            self.seen_tags = restored
        except Exception as exc:
            logger.warning("⚠️ Не удалось загрузить кэш дедупликации: %s", exc)

    def _save_cache(self):
        if not self.cache_file:
            return
        try:
            payload = {self._serialize_key(k): v for k, v in self.seen_tags.items()}
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("⚠️ Не удалось сохранить кэш: %s", exc)

    def should_save(self, epc: str, ant: int) -> bool:
        now = time.time()
        key = (epc, ant)
        last = self.seen_tags.get(key)
        if last is not None and now - last < self.ttl:
            return False
        self.seen_tags[key] = now
        return True

    def cleanup(self):
        now = time.time()
        cutoff = now - (self.ttl * 2)
        self.seen_tags = {k: v for k, v in self.seen_tags.items() if v > cutoff}


def parse_buf(buf, length: int) -> Optional[Dict]:
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
        epc_start = 3
        epc_hex_len = uii_len * 2 - 4
        epc_end = epc_start + (epc_hex_len // 2)
        if epc_end > len(d):
            return None
        epc = "".join(f"{b:02X}" for b in d[epc_start:epc_end])
        tid = "".join(f"{b:02X}" for b in d[tid_start:tid_start + tid_len]) if tid_len > 0 else ""
        rssi_raw = (d[rssi_idx] << 8) | d[rssi_idx + 1]
        rssi = (rssi_raw - 65535) / 10.0
        ant = d[ant_idx]
        return {"epc": epc, "tid": tid, "rssi": rssi, "ant": ant}
    except Exception as exc:
        logger.debug("🔍 Ошибка парсинга буфера: %s", exc)
        return None


class DBWriter:
    def __init__(self, conn_str: str, fallback_log: str):
        self.conn_str = conn_str
        self.fallback_log = fallback_log
        self.conn = None
        self.cursor = None

    def ensure_connection(self) -> bool:
        try:
            import pyodbc
        except ImportError:
            logger.critical("❌ Не установлен pyodbc. Выполните: pip install pyodbc")
            return False

        try:
            if self.conn is not None and self.cursor is not None:
                self.cursor.execute("SELECT 1")
                self.cursor.fetchone()
                return True
        except Exception:
            self.close()

        try:
            self.conn = pyodbc.connect(self.conn_str, timeout=10)
            self.cursor = self.conn.cursor()
            logger.info("✅ БД: подключение/переподключение успешно")
            return True
        except Exception as exc:
            logger.error("❌ БД: ошибка подключения: %s", exc)
            self.close()
            return False

    def close(self):
        try:
            if self.cursor is not None:
                self.cursor.close()
        except Exception:
            pass
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.cursor = None
        self.conn = None

    def _write_fallback_log(self, tags: List[Dict], reason: str):
        try:
            with open(self.fallback_log, "a", encoding="utf-8") as f:
                for tag in tags:
                    line = "|".join([
                        datetime.now().isoformat(),
                        tag["tag_time"],
                        str(tag["data"]["ant"]),
                        str(round(tag["data"]["rssi"], 1)),
                        tag["data"]["epc"],
                        tag["data"].get("tid", ""),
                        reason,
                    ]) + "\n"
                    f.write(line)
            logger.warning("💾 %s записей сохранены в %s", len(tags), self.fallback_log)
        except Exception as exc:
            logger.critical("💥 КРИТИЧЕСКИ: не удалось записать fallback-лог: %s", exc)

    def replay_fallback(self, max_lines: int = 1000):
        path = Path(self.fallback_log)
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            logger.warning("⚠️ Не удалось прочитать fallback-лог: %s", exc)
            return
        if not lines:
            return

        to_replay = lines[:max_lines]
        rest = lines[max_lines:]
        tags = []
        for line in to_replay:
            try:
                _, tag_time, ant, rssi, epc, tid, _reason = line.split("|", 6)
                tags.append({
                    "tag_time": tag_time,
                    "data": {"ant": int(ant), "rssi": float(rssi), "epc": epc, "tid": tid},
                })
            except Exception:
                continue

        if not tags:
            return

        written = self.insert_tags(tags, fallback_reason="REPLAY")
        if written == len(tags):
            path.write_text("\n".join(rest) + ("\n" if rest else ""), encoding="utf-8")
            logger.info("♻️ fallback-лог переотправлен: %s записей", written)
        else:
            logger.warning("⚠️ fallback-лог переотправлен частично: %s/%s", written, len(tags))

    def insert_tags(self, tags: List[Dict], fallback_reason: str = "DB_ERROR") -> int:
        if not tags:
            return 0
        if not self.ensure_connection():
            self._write_fallback_log(tags, fallback_reason)
            return 0

        written = 0
        try:
            for tag in tags:
                self.cursor.execute(
                    """
                    INSERT INTO dbo.RFID_Tags (RecordTime, TagTime, Antenna, RSSI, EPC, TID)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.now(),
                        tag["tag_time"],
                        int(tag["data"]["ant"]),
                        round(tag["data"]["rssi"], 1),
                        tag["data"]["epc"],
                        tag["data"]["tid"] or None,
                    ),
                )
                written += 1
            self.conn.commit()
            logger.info("📤 БД: записано %s из %s тегов", written, len(tags))
            return written
        except Exception as exc:
            logger.error("❌ БД: ошибка записи: %s", exc)
            try:
                self.conn.rollback()
            except Exception:
                pass
            self.close()
            remaining = tags[written:] if written < len(tags) else tags
            self._write_fallback_log(remaining, f"{fallback_reason}:{str(exc)[:100]}")
            return written


def test_db_connection(conn_str: str) -> bool:
    try:
        import pyodbc
        with pyodbc.connect(conn_str, timeout=5) as conn:
            conn.cursor().execute("SELECT 1")
        logger.info("✅ БД: подключение успешно")
        return True
    except ImportError:
        logger.critical("❌ Не установлен pyodbc. Выполните: pip install pyodbc")
        return False
    except Exception as exc:
        logger.error("❌ БД: ошибка подключения: %s", exc)
        return False


def load_library():
    try:
        lib = ctypes.CDLL(Config.DLL_PATH)
    except Exception as exc:
        logger.error("❌ Ошибка загрузки DLL: %s", exc)
        logger.error("💡 Убедитесь, что разрядность Python и DLL совпадает (32/64 бит)")
        return None

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
            ctypes.POINTER(ctypes.c_ubyte * 512),
        ]
        lib.UHF_GetReceived_EX.restype = ctypes.c_int
    except Exception as exc:
        logger.error("❌ Ошибка настройки прототипов DLL: %s", exc)
        return None
    return lib


def connect_reader(lib) -> bool:
    logger.info("🔌 Подключение к %s:%s...", Config.READER_IP, Config.READER_PORT)
    rc = lib.TCPConnect(Config.READER_IP.encode("utf-8"), Config.READER_PORT)
    if rc != 0:
        logger.error("❌ Не удалось подключиться к считывателю, код=%s", rc)
        return False
    inv_rc = lib.UHFInventory()
    if inv_rc != 0:
        logger.warning("⚠️ UHFInventory вернул код=%s", inv_rc)
    logger.info("✅ Подключено к считывателю")
    return True


def disconnect_reader(lib):
    try:
        lib.UHFStopGet()
    except Exception:
        pass
    try:
        lib.TCPDisconnect()
    except Exception:
        pass


def main() -> int:
    if not check_env():
        return 1
    Config.load()

    logger.info("🚀 Запуск: %s:%s", Config.READER_IP, Config.READER_PORT)
    logger.info("🔁 Дедупликация: %s (TTL=%ss)", "ВКЛ" if Config.DEDUP_ENABLED else "ВЫКЛ", Config.DEDUP_TTL)
    logger.info("📦 Пакет: BATCH_SIZE=%s, FLUSH_INTERVAL=%ss", Config.BATCH_SIZE, Config.BATCH_FLUSH_INTERVAL)

    if not test_db_connection(Config.DB_CONN_STR):
        logger.error("❌ Запуск невозможен без подключения к БД")
        return 1

    if not os.path.exists(Config.DLL_PATH):
        logger.error("❌ DLL не найдена: %s", Config.DLL_PATH)
        return 1

    lib = load_library()
    if lib is None:
        return 1

    writer = DBWriter(Config.DB_CONN_STR, Config.FALLBACK_LOG)
    writer.replay_fallback()

    deduplicator = TagDeduplicator(
        ttl_seconds=Config.DEDUP_TTL,
        cache_file=Config.DEDUP_CACHE_FILE if Config.DEDUP_ENABLED else None,
    )
    if Config.DEDUP_ENABLED and Config.DEDUP_CACHE_FILE:
        atexit.register(deduplicator._save_cache)

    if Config.CONSOLE_OUTPUT:
        print("\n" + "=" * 100)
        print(f"{'ВРЕМЯ':<12} {'АНТ':<4} {'RSSI':<8} {'EPC':<48} {'СТАТУС':<10}")
        print("=" * 100)
        sys.stdout.flush()

    batch: List[Dict] = []
    last_flush = time.time()
    last_cleanup = time.time()

    try:
        while True:
            if not connect_reader(lib):
                logger.warning("🔌 Отключено от считывателя, повторная попытка через %s сек", Config.RECONNECT_DELAY_SEC)
                time.sleep(Config.RECONNECT_DELAY_SEC)
                continue

            try:
                while True:
                    uLen = ctypes.c_int(0)
                    buf = (ctypes.c_ubyte * 512)()
                    res = lib.UHF_GetReceived_EX(ctypes.byref(uLen), ctypes.byref(buf))

                    if res == 0 and uLen.value > 0:
                        tag_data = parse_buf(buf, uLen.value)
                        if tag_data:
                            tag_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                            epc = tag_data["epc"]
                            ant = tag_data["ant"]

                            if Config.DEDUP_ENABLED and not deduplicator.should_save(epc, ant):
                                if Config.CONSOLE_OUTPUT:
                                    print(f"{tag_time:<12} {ant:<4} {tag_data['rssi']:<8.1f} {epc:<48} {'⊘ дубль':<10}")
                                    sys.stdout.flush()
                                time.sleep(Config.READ_SLEEP_SEC)
                                continue

                            item = {"data": tag_data, "tag_time": tag_time}
                            batch.append(item)

                            db_written = 0
                            if len(batch) >= Config.BATCH_SIZE:
                                db_written = writer.insert_tags(batch)
                                batch.clear()

                            if Config.CONSOLE_OUTPUT:
                                status = "✓ запис" if db_written > 0 else "… в буфере"
                                print(f"{tag_time:<12} {ant:<4} {tag_data['rssi']:<8.1f} {epc:<48} {status:<10}")
                                sys.stdout.flush()
                    else:
                        if batch and (time.time() - last_flush) >= Config.BATCH_FLUSH_INTERVAL:
                            written = writer.insert_tags(batch)
                            if Config.CONSOLE_OUTPUT and written > 0:
                                print(f"🟢 БД:+{written} ", end="", flush=True)
                            batch.clear()
                            last_flush = time.time()
                        time.sleep(Config.IDLE_SLEEP_SEC)

                    if Config.DEDUP_ENABLED and (time.time() - last_cleanup) >= 60:
                        deduplicator.cleanup()
                        last_cleanup = time.time()

                    time.sleep(Config.READ_SLEEP_SEC)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.exception("💥 Ошибка чтения со считывателя: %s", exc)
                try:
                    if batch:
                        writer.insert_tags(batch, fallback_reason="READ_LOOP_ERROR")
                        batch.clear()
                except Exception:
                    pass
                disconnect_reader(lib)
                logger.warning("🔌 Отключено от считывателя, повторная попытка через %s сек", Config.RECONNECT_DELAY_SEC)
                time.sleep(Config.RECONNECT_DELAY_SEC)

    except KeyboardInterrupt:
        logger.info("⏹️ Остановка по запросу пользователя (Ctrl+C)")
    except Exception as exc:
        logger.exception("💥 Неожиданная ошибка: %s", exc)
        return 1
    finally:
        try:
            if batch:
                written = writer.insert_tags(batch, fallback_reason="SHUTDOWN")
                logger.info("📤 Завершение: записано ещё %s записей", written)
        except Exception:
            pass
        try:
            disconnect_reader(lib)
        except Exception:
            pass
        writer.close()
        logger.info("👋 Завершение")

    return 0


if __name__ == "__main__":
    sys.exit(main())
