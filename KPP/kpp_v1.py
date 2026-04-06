#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Умный КПП - Мониторинг в реальном времени v4.0
Исправления: фильтрация по времени, умный консенсус, меньше спама
"""

import pyodbc
import time
from datetime import datetime, timedelta
from typing import Dict, List, Set
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict
import hashlib

# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

class Config:
    # Таймауты
    RFID_DISAPPEAR_TIMEOUT = 120     # 2 минуты - метка пропала
    RFID_MIN_ACCUMULATION = 10       # Минимум секунд накопления перед анализом
    POLL_INTERVAL = 5                # Опрос БД каждые 5 секунд
    SESSION_MAX_DURATION = 900       # 15 минут макс
    
    # Временные окна для корреляции (относительно RFID события)
    VIDEO_WINDOW_BEFORE = 120
    VIDEO_WINDOW_AFTER = 120
    SKUD_WINDOW_BEFORE = 180
    SKUD_WINDOW_AFTER = 180
    
    # Фильтр времени - читаем только свежие данные
    RFID_FRESH_WINDOW = 300          # Только RFID за последние 5 минут
    
    # Минимум для доверия
    MIN_RFID_READS_SINGLE = 1        # Достаточно 1 считывания если есть подтверждение
    MIN_RFID_READS_CONFIDENT = 3     # 3+ считывания = уверенно
    
    # Антенны
    OUTER_ANTENNAS = {2, 3}
    INNER_ANTENNAS = {1, 4}
    
    # Подключения
    KPP_CONN_STR = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        "SERVER=SRV-SQL4.MKM.LAN;"
        "DATABASE=1CTgSend;"
        "UID=TgSendUser;"
        "PWD=Shu_uc3i;"  # ⚠️ ВСТАВЬТЕ ПАРОЛЬ
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
    )
    
    ONEC_CONN_STR = (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        "SERVER=sql99,1433;"
        "DATABASE=msscada;"
        "UID=msscadaro;"
        "PWD=msscadaro"
    )

# ============================================================================
# МОДЕЛИ
# ============================================================================

class Direction(Enum):
    UNKNOWN = "НЕ ОПРЕДЕЛЕНО"
    IN = "ВЪЕЗД"
    OUT = "ВЫЕЗД"

class TransportMode(Enum):
    UNKNOWN = "НЕИЗВЕСТНО"
    ALONE = "САМА"
    HUMAN = "ЧЕЛОВЕК"
    FORKLIFT = "ПОГРУЗЧИК"
    FORKLIFT_HUMAN = "ПОГРУЗЧИК+ЧЕЛОВЕК"

class ConsensusResult(Enum):
    STRONG = "УВЕРЕННО"         # RFID 2+ зоны + подтверждение
    CONFIRMED = "ПОДТВЕРЖДЕНО"  # 2 системы согласны
    WEAK = "СЛАБО"              # 1 система + мало данных
    CONFLICT = "ПРОТИВОРЕЧИЕ"   # Системы противоречат

@dataclass
class Rfid1CTask:
    Id: int
    Dt: datetime
    Tag: str
    Ids: str
    EPC: str = ""
    TID: str = ""
    
    def __post_init__(self):
        self.Tag = self.Tag.upper().strip()
        if len(self.Tag) >= 48:
            self.EPC = self.Tag[:24]
            self.TID = self.Tag[24:]
        elif len(self.Tag) >= 24:
            self.EPC = self.Tag[:24]
            self.TID = self.Tag[24:] if len(self.Tag) > 24 else ""

@dataclass
class RfidRead:
    Id: int
    RecordTime: datetime
    Antenna: int
    RSSI: float
    EPC: str
    TID: str
    
    @property
    def FullTag(self) -> str:
        return (self.EPC + self.TID).upper()
    
    @property
    def Zone(self) -> str:
        if self.Antenna in Config.OUTER_ANTENNAS:
            return "OUTER"
        elif self.Antenna in Config.INNER_ANTENNAS:
            return "INNER"
        return "UNKNOWN"

@dataclass
class ActiveSession:
    task: Rfid1CTask
    first_seen: datetime
    last_seen: datetime
    reads: List[RfidRead] = field(default_factory=list)
    antennas_seen: Set[int] = field(default_factory=set)
    zones_seen: Set[str] = field(default_factory=set)
    is_complete: bool = False
    completed_at: datetime = None
    has_crossing_evidence: bool = False  # Есть ли признаки пересечения зон

@dataclass
class CrossingReport:
    session_id: str
    report_time: datetime
    event_time_start: datetime
    event_time_end: datetime
    task_id: int
    task_ids: str
    epc: str
    tid: str
    
    direction: Direction
    confidence_percent: int
    transport_mode: TransportMode
    consensus: ConsensusResult
    
    rfid_reads: int
    rfid_antennas: List[int]
    rfid_zones: List[str]
    rfid_avg_rssi: float
    
    video_found: bool
    video_direction: str
    video_transport: str
    video_count: int
    
    skud_found: bool
    skud_direction: str
    skud_gate: str
    skud_person: str
    skud_count: int
    
    final_decision: str
    warnings: List[str] = field(default_factory=list)
    is_likely_crossing: bool = True  # Вероятно ли реальное пересечение

# ============================================================================
# МЕНЕДЖЕР
# ============================================================================

class KPPMonitor:
    def __init__(self):
        self.tasks: Dict[str, Rfid1CTask] = {}
        self.active_sessions: Dict[str, ActiveSession] = {}
        self.completed_reports: List[CrossingReport] = []
        self.last_rfid_id = 0
        self.start_time = datetime.now()
        self.last_status_print = None
        self.last_completed_count = 0
        
    def load_1c_tasks(self) -> int:
        print(f"📋 Загрузка заданий из 1С...")
        
        query = """
        SELECT TOP 1000 Id, Dt, Tag, Ids 
        FROM Rfid1С 
        WHERE Dt >= DATEADD(hour, -24, GETDATE())
        ORDER BY Dt DESC
        """
        
        with pyodbc.connect(Config.ONEC_CONN_STR) as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
        
        self.tasks.clear()
        for row in rows:
            task = Rfid1CTask(
                Id=int(row[0]),
                Dt=row[1],
                Tag=str(row[2]).upper().strip(),
                Ids=str(row[3]) if row[3] else ""
            )
            self.tasks[task.Tag] = task
        
        print(f"   ✓ Загружено {len(self.tasks)} заданий")
        return len(self.tasks)
    
    def get_new_rfid_reads(self) -> List[RfidRead]:
        """Получаем ТОЛЬКО свежие RFID события (за последние 5 минут)"""
        try:
            # Фильтруем по времени + по ID для инкрементальности
            fresh_time = datetime.now() - timedelta(seconds=Config.RFID_FRESH_WINDOW)
            
            query = """
            SELECT TOP 500 Id, RecordTime, Antenna, RSSI, EPC, TID
            FROM RFID_Tags
            WHERE Id > ? AND RecordTime >= ?
            ORDER BY Id ASC
            """
            
            with pyodbc.connect(Config.KPP_CONN_STR) as conn:
                cursor = conn.cursor()
                cursor.execute(query, self.last_rfid_id, fresh_time)
                rows = cursor.fetchall()
            
            reads = []
            for row in rows:
                read = RfidRead(
                    Id=int(row[0]),
                    RecordTime=row[1],
                    Antenna=int(row[2]),
                    RSSI=float(row[3]) if row[3] else 0.0,
                    EPC=str(row[4]).upper().strip() if row[4] else "",
                    TID=str(row[5]).upper().strip() if row[5] else ""
                )
                reads.append(read)
                self.last_rfid_id = max(self.last_rfid_id, read.Id)
            
            return reads
        except Exception as e:
            print(f"❌ Ошибка RFID: {e}")
            return []
    
    def get_video_events(self, start: datetime, end: datetime) -> List[dict]:
        try:
            query = """
            SELECT Id, Timestamp, Direction, FromCamera, ToCamera, TransportMode
            FROM ReelTransitions
            WHERE Timestamp BETWEEN ? AND ?
            ORDER BY Timestamp
            """
            
            with pyodbc.connect(Config.KPP_CONN_STR) as conn:
                cursor = conn.cursor()
                cursor.execute(query, start, end)
                rows = cursor.fetchall()
            
            return [{
                'time': row[1],
                'direction': str(row[2]) if row[2] else "",
                'transport': str(row[5]) if row[5] else ""
            } for row in rows]
        except:
            return []
    
    def get_skud_events(self, start: datetime, end: datetime) -> List[dict]:
        try:
            query = """
            SELECT ExternalId2, CreatedAt, Direction, PersonControlDeviceName, 
                   FullName, CardNumReal
            FROM RusGuardLogs
            WHERE CreatedAt BETWEEN ? AND ?
            ORDER BY CreatedAt
            """
            
            with pyodbc.connect(Config.KPP_CONN_STR) as conn:
                cursor = conn.cursor()
                cursor.execute(query, start, end)
                rows = cursor.fetchall()
            
            return [{
                'time': row[1],
                'direction': str(row[2]) if row[2] else "",
                'gate': str(row[3]) if row[3] else "",
                'person': str(row[4]) if row[4] else ""
            } for row in rows]
        except:
            return []
    
    def process_rfid_read(self, read: RfidRead):
        """Обрабатываем считывание"""
        # Ищем задачу по полному тегу или по EPC
        task = self.tasks.get(read.FullTag)
        
        if not task:
            # Пробуем по EPC
            for tag, t in self.tasks.items():
                if t.EPC and read.EPC == t.EPC:
                    task = t
                    break
        
        if not task:
            return  # Не наша метка
        
        tag = read.FullTag
        
        if tag not in self.active_sessions:
            self.active_sessions[tag] = ActiveSession(
                task=task,
                first_seen=read.RecordTime,
                last_seen=read.RecordTime
            )
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🆕 НОВАЯ СЕССИЯ: {tag[:35]}... (Антенна {read.Antenna})")
        
        session = self.active_sessions[tag]
        session.reads.append(read)
        session.last_seen = read.RecordTime
        session.antennas_seen.add(read.Antenna)
        session.zones_seen.add(read.Zone)
        
        # Проверяем признаки пересечения (2+ зоны)
        if len(session.zones_seen) >= 2:
            session.has_crossing_evidence = True
    
    def check_completed_sessions(self) -> List[ActiveSession]:
        """Проверяем завершенные сессии"""
        completed = []
        now = datetime.now()
        
        for tag, session in list(self.active_sessions.items()):
            time_since_last = (now - session.last_seen).total_seconds()
            session_duration = (session.last_seen - session.first_seen).total_seconds()
            accumulation_time = session_duration
            
            # Условия завершения
            is_timeout = time_since_last > Config.RFID_DISAPPEAR_TIMEOUT
            is_max_duration = session_duration > Config.SESSION_MAX_DURATION
            is_ready = accumulation_time >= Config.RFID_MIN_ACCUMULATION
            
            if (is_timeout or is_max_duration) and is_ready:
                session.is_complete = True
                session.completed_at = now
                completed.append(session)
                del self.active_sessions[tag]
                
                reason = "таймаут" if is_timeout else "макс.длительность"
                print(f"[{now.strftime('%H:%M:%S')}] ✅ СЕССИЯ ЗАВЕРШЕНА: {tag[:35]}... ({reason}, {len(session.reads)} считываний, {session_duration:.0f}сек)")
        
        return completed
    
    def analyze_session(self, session: ActiveSession) -> CrossingReport:
        """Анализируем сессию"""
        task = session.task
        reads = sorted(session.reads, key=lambda x: x.RecordTime)
        antennas = sorted(session.antennas_seen)
        zones = sorted(session.zones_seen)
        
        # RFID направление
        rfid_direction = Direction.UNKNOWN
        if len(reads) >= 2:
            first_zone = reads[0].Zone
            last_zone = reads[-1].Zone
            if first_zone == "OUTER" and last_zone == "INNER":
                rfid_direction = Direction.IN
            elif first_zone == "INNER" and last_zone == "OUTER":
                rfid_direction = Direction.OUT
        
        avg_rssi = sum(r.RSSI for r in reads) / len(reads) if reads else 0
        
        # Видео
        video_start = session.first_seen - timedelta(seconds=Config.VIDEO_WINDOW_BEFORE)
        video_end = session.last_seen + timedelta(seconds=Config.VIDEO_WINDOW_AFTER)
        video_events = self.get_video_events(video_start, video_end)
        
        video_direction = ""
        video_transport = ""
        if video_events:
            directions = [v['direction'] for v in video_events]
            video_direction = max(set(directions), key=directions.count) if directions else ""
            transports = [v['transport'] for v in video_events]
            video_transport = max(set(transports), key=transports.count) if transports else ""
        
        # СКУД
        skud_start = session.first_seen - timedelta(seconds=Config.SKUD_WINDOW_BEFORE)
        skud_end = session.last_seen + timedelta(seconds=Config.SKUD_WINDOW_AFTER)
        skud_events = self.get_skud_events(skud_start, skud_end)
        
        skud_direction = ""
        skud_gate = ""
        skud_person = ""
        if skud_events:
            skud_direction = skud_events[0]['direction']
            skud_gate = skud_events[0]['gate']
            skud_person = skud_events[0]['person']
        
        # === УМНЫЙ КОНСЕНСУС ===
        votes = {'IN': 0, 'OUT': 0}
        systems_with_data = 0
        
        # RFID голос (только если 2+ зоны или 3+ считывания)
        if len(zones) >= 2 or len(reads) >= Config.MIN_RFID_READS_CONFIDENT:
            if rfid_direction != Direction.UNKNOWN:
                votes[rfid_direction.name] += 1
            systems_with_data += 1
        
        # Видео голос
        if video_events:
            if '0>1' in video_direction or video_direction == 'IN':
                votes['IN'] += 1
            elif '1>0' in video_direction or video_direction == 'OUT':
                votes['OUT'] += 1
            systems_with_data += 1
        
        # СКУД голос (только если есть RFID подтверждение или ворота открылись)
        if skud_events:
            if skud_direction == 'IN':
                votes['IN'] += 1
            elif skud_direction == 'OUT':
                votes['OUT'] += 1
            systems_with_data += 1
        
        # Финальное направление
        final_direction = Direction.UNKNOWN
        if votes['IN'] > votes['OUT']:
            final_direction = Direction.IN
        elif votes['OUT'] > votes['IN']:
            final_direction = Direction.OUT
        
        # Уверенность и консенсус
        confidence = 0
        consensus = ConsensusResult.WEAK
        is_likely_crossing = False
        
        if len(zones) >= 2 and systems_with_data >= 2:
            consensus = ConsensusResult.STRONG
            confidence = 90
            is_likely_crossing = True
        elif systems_with_data >= 2 and max(votes.values()) >= 2:
            consensus = ConsensusResult.CONFIRMED
            confidence = 70
            is_likely_crossing = True
        elif systems_with_data >= 1 and (len(reads) >= 1 or video_events or skud_events):
            consensus = ConsensusResult.WEAK
            confidence = 50
            is_likely_crossing = len(reads) >= 1 and (video_events or skud_events)
        else:
            consensus = ConsensusResult.WEAK
            confidence = 30
            is_likely_crossing = False
        
        # Проверка на противоречия
        if votes['IN'] > 0 and votes['OUT'] > 0:
            consensus = ConsensusResult.CONFLICT
            confidence = 40
        
        # Транспорт
        transport = TransportMode.UNKNOWN
        if video_transport:
            tt = video_transport.lower()
            if 'forklift' in tt and 'human' in tt:
                transport = TransportMode.FORKLIFT_HUMAN
            elif 'forklift' in tt or 'погрузчик' in tt:
                transport = TransportMode.FORKLIFT
            elif 'human' in tt or 'человек' in tt:
                transport = TransportMode.HUMAN
            elif 'alone' in tt:
                transport = TransportMode.ALONE
        
        # Предупреждения
        warnings = []
        if len(reads) < Config.MIN_RFID_READS_CONFIDENT:
            warnings.append(f"⚠️ Мало считываний RFID ({len(reads)})")
        if len(zones) < 2:
            warnings.append(f"⚠️ Одна зона антенн ({zones}) - возможно не пересечение")
        if consensus == ConsensusResult.CONFLICT:
            warnings.append(f"⚠️ Противоречие направлений между системами")
        if not video_events and not skud_events:
            warnings.append(f"⚠️ Нет подтверждения от ВИДЕО/СКУД")
        if not is_likely_crossing:
            warnings.append(f"⚠️ Низкая вероятность реального пересечения")
        
        return CrossingReport(
            session_id=hashlib.md5(f"{task.Id}{session.first_seen}".encode()).hexdigest()[:12],
            report_time=datetime.now(),
            event_time_start=session.first_seen,
            event_time_end=session.last_seen,
            task_id=task.Id,
            task_ids=task.Ids,
            epc=task.EPC,
            tid=task.TID,
            direction=final_direction,
            confidence_percent=confidence,
            transport_mode=transport,
            consensus=consensus,
            rfid_reads=len(reads),
            rfid_antennas=antennas,
            rfid_zones=zones,
            rfid_avg_rssi=round(avg_rssi, 1),
            video_found=len(video_events) > 0,
            video_direction=video_direction,
            video_transport=video_transport,
            video_count=len(video_events),
            skud_found=len(skud_events) > 0,
            skud_direction=skud_direction,
            skud_gate=skud_gate,
            skud_person=skud_person,
            skud_count=len(skud_events),
            final_decision=final_direction.value,
            warnings=warnings,
            is_likely_crossing=is_likely_crossing
        )
    
    def print_report(self, report: CrossingReport):
        """Вывод отчета"""
        # Пропускаем слабые отчеты если нужно (можно добавить фильтр)
        # if not report.is_likely_crossing:
        #     return
        
        print("\n" + "═"*100)
        print(f"📦 ПЕРЕСЕЧЕНИЕ КПП: {report.session_id}")
        print("═"*100)
        print(f"⏰ Событие:         {report.event_time_start.strftime('%Y-%m-%d %H:%M:%S')} - {report.event_time_end.strftime('%H:%M:%S')}")
        print(f"📋 Отчет создан:    {report.report_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"🏷️  Метка:           {report.epc}{report.tid}")
        print(f"🔑 EPC:              {report.epc}")
        print(f"🔑 TID:              {report.tid}")
        print(f"📄 1С ID:            {report.task_id}")
        print(f"📄 1С Документ:      {report.task_ids}")
        
        status_icon = "✅" if report.is_likely_crossing else "⚠️"
        print(f"\n{status_icon} 🎯 РЕШЕНИЕ:")
        dir_icon = "➡️" if report.direction == Direction.IN else "⬅️" if report.direction == Direction.OUT else "❓"
        print(f"   Направление:      {dir_icon} {report.final_decision}")
        print(f"   Уверенность:      {report.confidence_percent}%")
        print(f"   Консенсус:        {report.consensus.value}")
        print(f"   Транспорт:        {report.transport_mode.value}")
        print(f"   Пересечение:      {'ДА' if report.is_likely_crossing else 'СОМНИТЕЛЬНО'}")
        
        print(f"\n📡 RFID ({report.rfid_reads} считываний):")
        print(f"   Длительность:     {(report.event_time_end - report.event_time_start).total_seconds():.1f} сек")
        print(f"   Антенны:          {report.rfid_antennas}")
        print(f"   Зоны:             {report.rfid_zones}")
        print(f"   Средний RSSI:     {report.rfid_avg_rssi} dBm")
        
        print(f"\n📹 ВИДЕО:")
        status = "✅" if report.video_found else "❌"
        print(f"   {status} Событий: {report.video_count}")
        if report.video_found:
            print(f"   Направление:      {report.video_direction}")
            print(f"   Транспорт:        {report.video_transport}")
        
        print(f"\n🚪 СКУД:")
        status = "✅" if report.skud_found else "❌"
        print(f"   {status} Событий: {report.skud_count}")
        if report.skud_found:
            print(f"   Направление:      {report.skud_direction}")
            print(f"   Ворота:           {report.skud_gate}")
            print(f"   Лицо/Авто:        {report.skud_person}")
        
        if report.warnings:
            print(f"\n⚠️ ПРЕДУПРЕЖДЕНИЯ:")
            for w in report.warnings:
                print(f"   {w}")
        
        print("═"*100)
    
    def print_status(self, force: bool = False):
        """Вывод статуса (только при изменениях)"""
        now = datetime.now()
        
        # Не чаще чем раз в 30 секунд
        if self.last_status_print and (now - self.last_status_print).total_seconds() < 30:
            if not force:
                return
        
        # Только если изменилось количество завершенных
        if len(self.completed_reports) == self.last_completed_count and not force:
            return
        
        self.last_status_print = now
        self.last_completed_count = len(self.completed_reports)
        
        uptime = (now - self.start_time).total_seconds() / 60
        print(f"\n[{'now.strftime('%H:%M:%S')}] 📊 СТАТУС: Активных={len(self.active_sessions)} | Завершено={len(self.completed_reports)} | RFID_ID={self.last_rfid_id} | Время={uptime:.0f}мин")
    
    def run(self):
        """Основной цикл"""
        print("\n" + "█"*100)
        print("🚀 УМНЫЙ КПП - МОНИТОРИНГ В РЕАЛЬНОМ ВРЕМЕНИ v4.0")
        print("█"*100)
        print(f"⏰ Запуск: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📊 Таймаут: {Config.RFID_DISAPPEAR_TIMEOUT} сек | Окно RFID: {Config.RFID_FRESH_WINDOW} сек")
        print(f"📊 Опрос: {Config.POLL_INTERVAL} сек | Мин.накопление: {Config.RFID_MIN_ACCUMULATION} сек")
        print("█"*100)
        
        tasks_count = self.load_1c_tasks()
        
        if tasks_count == 0:
            print("❌ НЕТ ЗАДАНИЙ ИЗ 1С!")
            return
        
        print("\n✅ МОНИТОРИНГ ЗАПУЩЕН (Ctrl+C для остановки)\n")
        
        try:
            while True:
                # 1. Новые RFID
                new_reads = self.get_new_rfid_reads()
                for read in new_reads:
                    self.process_rfid_read(read)
                
                # 2. Завершенные сессии
                completed = self.check_completed_sessions()
                for session in completed:
                    report = self.analyze_session(session)
                    self.print_report(report)
                    self.completed_reports.append(report)
                
                # 3. Статус
                self.print_status()
                
                # 4. Ждем
                time.sleep(Config.POLL_INTERVAL)
                
        except KeyboardInterrupt:
            print("\n\n⏹️  Остановка...")
        except Exception as e:
            print(f"\n❌ ОШИБКА: {e}")
            import traceback
            traceback.print_exc()
        
        # Итоги
        print("\n" + "█"*100)
        print("📊 ИТОГИ")
        print("█"*100)
        print(f"⏰ Работа: {(datetime.now() - self.start_time).total_seconds()/60:.1f} мин")
        print(f"📦 Завершено: {len(self.completed_reports)}")
        
        if self.completed_reports:
            confirmed = sum(1 for r in self.completed_reports if r.is_likely_crossing)
            in_count = sum(1 for r in self.completed_reports if r.direction == Direction.IN)
            out_count = sum(1 for r in self.completed_reports if r.direction == Direction.OUT)
            
            print(f"   Подтверждено пересечений: {confirmed}/{len(self.completed_reports)}")
            print(f"   Въездов: {in_count} | Выездов: {out_count}")
        
        print("█"*100)

if __name__ == "__main__":
    monitor = KPPMonitor()
    monitor.run()