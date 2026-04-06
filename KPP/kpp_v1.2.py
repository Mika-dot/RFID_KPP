#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Умный КПП - Мониторинг в реальном времени
Версия: 5.1 (Исправлена ошибка dataclass)
Запуск: python kpp_monitor.py
"""

import pyodbc
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict, Counter
import hashlib
import sys

# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

class Config:
    # Таймауты (секунды)
    RFID_DISAPPEAR_TIMEOUT = 30      # Метка пропала = сессия завершена
    POLL_INTERVAL = 5                # Опрос БД каждые 5 секунд
    SESSION_MAX_DURATION = 600       # Макс длительность сессии (10 мин)
    TASKS_RELOAD_INTERVAL = 30       # Обновление задач из 1С каждые 30 сек
    
    # Временные окна для корреляции
    VIDEO_WINDOW_BEFORE = 60         # Видео за 60 сек до
    VIDEO_WINDOW_AFTER = 60          # Видео за 60 сек после
    SKUD_WINDOW_BEFORE = 120         # СКУД за 120 сек до
    SKUD_WINDOW_AFTER = 120          # СКУД за 120 сек после
    
    # Минимум событий для доверия
    MIN_RFID_READS = 2               # Минимум считываний RFID
    MIN_ANTENNA_ZONES = 2            # Минимум зон антенн для пересечения
    
    # Антенны
    OUTER_ANTENNAS = {2, 3}
    INNER_ANTENNAS = {1, 4}
    
    # Подключения
    KPP_CONN_STR = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        "SERVER=SRV-SQL4.MKM.LAN;"
        "DATABASE=1CTgSend;"
        "UID=TgSendUser;"
        "PWD=Shu_uc3i;"
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
    UNKNOWN = "UNKNOWN"
    IN = "ВЪЕЗД"
    OUT = "ВЫЕЗД"

class TransportMode(Enum):
    UNKNOWN = "НЕИЗВЕСТНО"
    ALONE = "САМА"
    HUMAN = "ЧЕЛОВЕК"
    FORKLIFT = "ПОГРУЗЧИК"

class ConsensusResult(Enum):
    UNANIMOUS = "ЕДИНОГЛАСНО"
    MAJORITY = "БОЛЬШИНСТВО"
    SPLIT = "РАЗНОГЛАСИЕ"
    NO_DATA = "НЕТ ДАННЫХ"

@dataclass
class Rfid1CTask:
    """Задание из 1С на отслеживание"""
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
    """Считывание RFID"""
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
    """Активная сессия отслеживания"""
    task: Rfid1CTask
    first_seen: datetime
    last_seen: datetime
    reads: List[RfidRead] = field(default_factory=list)
    antennas_seen: Set[int] = field(default_factory=set)
    zones_seen: Set[str] = field(default_factory=set)
    antenna_counts: Dict[int, int] = field(default_factory=lambda: defaultdict(int))
    is_complete: bool = False
    completed_at: datetime = None

@dataclass
class CrossingReport:
    """Итоговый отчет о пересечении"""
    # === ПОЛЯ БЕЗ ЗНАЧЕНИЙ ПО УМОЛЧАНИЮ (должны быть первыми) ===
    session_id: str
    timestamp: datetime
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
    rfid_first: datetime
    rfid_last: datetime
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
    
    # === ПОЛЯ СО ЗНАЧЕНИЯМИ ПО УМОЛЧАНИЮ (должны быть в конце) ===
    rfid_antenna_counts: Dict[int, int] = field(default_factory=dict)
    video_events: List[dict] = field(default_factory=list)
    skud_events: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

# ============================================================================
# МЕНЕДЖЕР МОНИТОРИНГА
# ============================================================================

class KPPMonitor:
    def __init__(self):
        self.tasks: Dict[str, Rfid1CTask] = {}
        self.active_sessions: Dict[str, ActiveSession] = {}
        self.completed_reports: List[CrossingReport] = []
        self.last_rfid_id = 0
        self.start_time = datetime.now()
        self.last_tasks_reload = None
        self.ignored_tags: Set[str] = set()
        
    def load_1c_tasks(self) -> int:
        """Загружаем актуальные задания из 1С (БЕЗ ЛИМИТОВ)"""
        try:
            query = """
            SELECT Id, Dt, Tag, Ids 
            FROM Rfid1С 
            WHERE Dt >= DATEADD(hour, -48, GETDATE())
            ORDER BY Dt DESC
            """
            
            with pyodbc.connect(Config.ONEC_CONN_STR) as conn:
                cursor = conn.cursor()
                cursor.execute(query)
                rows = cursor.fetchall()
            
            new_tasks = {}
            for row in rows:
                task = Rfid1CTask(
                    Id=int(row[0]),
                    Dt=row[1],
                    Tag=str(row[2]).upper().strip(),
                    Ids=str(row[3]) if row[3] else ""
                )
                new_tasks[task.Tag] = task
            
            if len(new_tasks) != len(self.tasks):
                self.tasks = new_tasks
                self._log(f"📋 Задач 1С: {len(self.tasks)} (обновлено)", "INFO")
            else:
                if set(new_tasks.keys()) != set(self.tasks.keys()):
                    self.tasks = new_tasks
                    self._log(f"📋 Задач 1С: {len(self.tasks)} (изменения)", "INFO")
            
            return len(self.tasks)
        except Exception as e:
            self._log(f"❌ Ошибка загрузки 1С: {e}", level="ERROR")
            return len(self.tasks)
    
    def get_new_rfid_reads(self) -> List[RfidRead]:
        """Получаем новые считывания RFID (БЕЗ ЛИМИТОВ)"""
        try:
            query = """
            SELECT Id, RecordTime, Antenna, RSSI, EPC, TID
            FROM RFID_Tags
            WHERE Id > ?
            ORDER BY Id ASC
            """
            
            with pyodbc.connect(Config.KPP_CONN_STR) as conn:
                cursor = conn.cursor()
                cursor.execute(query, self.last_rfid_id)
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
            
            if reads:
                self._log(f"📡 Получено {len(reads)} новых RFID считываний (ID: {reads[0].Id}-{reads[-1].Id})", "INFO")
            
            return reads
        except Exception as e:
            self._log(f"❌ Ошибка чтения RFID: {e}", level="ERROR")
            return []
    
    def get_video_events(self, start: datetime, end: datetime) -> List[dict]:
        """Получаем видео события за период (БЕЗ ЛИМИТОВ)"""
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
                'id': row[0],
                'time': row[1],
                'direction': str(row[2]) if row[2] else "",
                'from_cam': row[3],
                'to_cam': row[4],
                'transport': str(row[5]) if row[5] else ""
            } for row in rows]
        except Exception as e:
            self._log(f"⚠️ Ошибка чтения ВИДЕО: {e}", level="WARN")
            return []
    
    def get_skud_events(self, start: datetime, end: datetime) -> List[dict]:
        """Получаем события СКУД за период (БЕЗ ЛИМИТОВ)"""
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
                'id': row[0],
                'time': row[1],
                'direction': str(row[2]) if row[2] else "",
                'gate': str(row[3]) if row[3] else "",
                'person': str(row[4]) if row[4] else "",
                'card': str(row[5]) if row[5] else ""
            } for row in rows]
        except Exception as e:
            self._log(f"⚠️ Ошибка чтения СКУД: {e}", level="WARN")
            return []
    
    def process_rfid_read(self, read: RfidRead):
        """Обрабатываем одно считывание RFID"""
        tag = read.FullTag
        
        if tag in self.ignored_tags:
            return
        
        if tag not in self.tasks:
            found_task_tag = None
            for task_tag, task in self.tasks.items():
                if task.EPC and read.EPC == task.EPC:
                    found_task_tag = task_tag
                    break
            
            if found_task_tag:
                tag = found_task_tag
            else:
                if tag not in self.ignored_tags:
                    self._log(f"⚠️ ИГНОР RFID: {tag[:40]}... (Нет задачи в 1С, EPC: {read.EPC[:20]}...)", "WARN")
                    self.ignored_tags.add(tag)
                    if len(self.ignored_tags) > 1000:
                        self.ignored_tags.clear()
                return
        
        if tag not in self.active_sessions:
            task = self.tasks.get(tag)
            if not task:
                return
                
            self.active_sessions[tag] = ActiveSession(
                task=task,
                first_seen=read.RecordTime,
                last_seen=read.RecordTime
            )
            self._log(f"🆕 НОВАЯ СЕССИЯ: {tag[:30]}... (Антенна {read.Antenna}, Задача 1С #{task.Id})", "SUCCESS")
        
        session = self.active_sessions[tag]
        session.reads.append(read)
        session.last_seen = read.RecordTime
        session.antennas_seen.add(read.Antenna)
        session.zones_seen.add(read.Zone)
        session.antenna_counts[read.Antenna] += 1
    
    def check_completed_sessions(self) -> List[ActiveSession]:
        """Проверяем завершенные сессии (метка пропала)"""
        completed = []
        now = datetime.now()
        
        for tag, session in list(self.active_sessions.items()):
            time_since_last = (now - session.last_seen).total_seconds()
            session_duration = (session.last_seen - session.first_seen).total_seconds()
            
            is_timeout = time_since_last > Config.RFID_DISAPPEAR_TIMEOUT
            is_max_duration = session_duration > Config.SESSION_MAX_DURATION
            
            if is_timeout or is_max_duration:
                session.is_complete = True
                session.completed_at = now
                completed.append(session)
                del self.active_sessions[tag]
                
                reason = "таймаут" if is_timeout else "макс.длительность"
                self._log(f"✅ СЕССИЯ ЗАВЕРШЕНА: {tag[:30]}... ({reason}, {len(session.reads)} считываний)", "SUCCESS")
        
        return completed
    
    def analyze_session(self, session: ActiveSession) -> CrossingReport:
        """Анализируем сессию и создаем отчет"""
        task = session.task
        reads = session.reads
        
        # === RFID АНАЛИЗ ===
        reads.sort(key=lambda x: x.RecordTime)
        antennas = sorted(session.antennas_seen)
        zones = sorted(session.zones_seen)
        antenna_counts = dict(session.antenna_counts)
        
        # Направление по RFID
        rfid_direction = Direction.UNKNOWN
        first_zone = reads[0].Zone if reads else None
        last_zone = reads[-1].Zone if reads else None
        
        if first_zone == "OUTER" and last_zone == "INNER":
            rfid_direction = Direction.IN
        elif first_zone == "INNER" and last_zone == "OUTER":
            rfid_direction = Direction.OUT
        
        avg_rssi = sum(r.RSSI for r in reads) / len(reads) if reads else 0
        
        # === ВИДЕО АНАЛИЗ ===
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
        
        # === СКУД АНАЛИЗ ===
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
        
        # === КОНСЕНСУС ===
        directions_votes = {
            'IN': 0,
            'OUT': 0,
            'UNKNOWN': 0
        }
        systems_count = 0
        
        # RFID голос
        if rfid_direction != Direction.UNKNOWN and len(zones) >= Config.MIN_ANTENNA_ZONES:
            directions_votes[rfid_direction.name] += 1
            systems_count += 1
        
        # Видео голос
        if video_direction:
            if '0>1' in video_direction:  # 0(внутри)>1(снаружи) = ВЫЕЗД
                directions_votes['OUT'] += 1
            elif '1>0' in video_direction:  # 1(снаружи)>0(внутри) = ВЪЕЗД
                directions_votes['IN'] += 1
            else:
                directions_votes['UNKNOWN'] += 1
            systems_count += 1
        
        # СКУД голос
        if skud_direction:
            if skud_direction == 'IN':
                directions_votes['IN'] += 1
            elif skud_direction == 'OUT':
                directions_votes['OUT'] += 1
            else:
                directions_votes['UNKNOWN'] += 1
            systems_count += 1
        
        # Определяем консенсус
        # Определяем консенсус
        max_votes = max(directions_votes.values())
        unknown_votes = directions_votes['UNKNOWN']

        # Считаем сколько систем реально дали направление (IN или OUT)
        direction_systems = systems_count - unknown_votes

        if max_votes >= 2:
            consensus = ConsensusResult.MAJORITY
        elif direction_systems == 0:
            # Ни одна система не дала направления
            consensus = ConsensusResult.NO_DATA
        elif direction_systems == 1 and max_votes == 1:
            # Одна система дала направление, другие молчат (не противоречат)
            consensus = ConsensusResult.MAJORITY  # Доверяем единственному источнику
        elif direction_systems > 1 and max_votes == 1:
            # Несколько систем дали разные направления (противоречие)
            consensus = ConsensusResult.SPLIT
        else:
            consensus = ConsensusResult.UNANIMOUS if max_votes == direction_systems else ConsensusResult.MAJORITY
        
        # Финальное решение
        final_direction = Direction.UNKNOWN
        if directions_votes['IN'] > directions_votes['OUT']:
            final_direction = Direction.IN
        elif directions_votes['OUT'] > directions_votes['IN']:
            final_direction = Direction.OUT
        
        # Уверенность
        confidence = int((max_votes / max(systems_count, 1)) * 100) if systems_count > 0 else 0
        
        # Транспорт
        transport = TransportMode.UNKNOWN
        if video_transport:
            tt = video_transport.lower()
            if 'forklift' in tt or 'погрузчик' in tt.lower():
                transport = TransportMode.FORKLIFT
            elif 'human' in tt or 'человек' in tt.lower():
                transport = TransportMode.HUMAN
            elif 'alone' in tt:
                transport = TransportMode.ALONE
        
        # Предупреждения
        warnings = []
        if len(reads) < Config.MIN_RFID_READS:
            warnings.append(f"⚠️ Мало считываний RFID ({len(reads)})")
        if len(zones) < Config.MIN_ANTENNA_ZONES:
            warnings.append(f"⚠️ Только одна зона антенн ({zones})")
        if consensus == ConsensusResult.SPLIT:
            warnings.append(f"⚠️ Разногласие систем")
        if not video_events and not skud_events:
            warnings.append(f"⚠️ Нет подтверждения от ВИДЕО и СКУД")
        
        # Создаем отчет
        report = CrossingReport(
            session_id=hashlib.md5(f"{task.Id}{session.first_seen}".encode()).hexdigest()[:12],
            timestamp=datetime.now(),
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
            rfid_first=reads[0].RecordTime if reads else None,
            rfid_last=reads[-1].RecordTime if reads else None,
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
            final_decision=final_direction.value if final_direction != Direction.UNKNOWN else "НЕ ОПРЕДЕЛЕНО",
            rfid_antenna_counts=antenna_counts,
            video_events=video_events,
            skud_events=skud_events,
            warnings=warnings
        )
        
        return report
    
    def _format_direction_explanation(self, direction: str) -> str:
        """Объяснение кодов направления"""
        if not direction:
            return "Нет данных"
        
        explanations = {
            '0>1': '0>1 = ВЫЕЗД (из помещения → на улицу)',
            '1>0': '1>0 = ВЪЕЗД (с улицы → в помещение)',
            'IN': 'IN = ВЪЕЗД',
            'OUT': 'OUT = ВЫЕЗД',
            'Вход по ключу': 'Вход по ключу = ВЪЕЗД',
            'Выход по ключу': 'Выход по ключу = ВЫЕЗД',
        }
        
        return explanations.get(direction, direction)
    
    def print_report(self, report: CrossingReport):
        """Выводим отчет в консоль (РАСШИРЕННЫЙ)"""
        print("\n" + "═"*100)
        print(f"📦 ПЕРЕСЕЧЕНИЕ КПП: {report.session_id}")
        print("═"*100)
        print(f"⏰ Время отчета:     {report.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"🏷️  Метка (Tag):     {report.epc}{report.tid}")
        print(f"🔑 EPC:              {report.epc}")
        print(f"🔑 TID:              {report.tid}")
        print(f"📄 1С ID:            {report.task_id}")
        print(f"📄 1С Документ:      {report.task_ids}")
        
        print(f"\n🎯 РЕШЕНИЕ:")
        dir_icon = "➡️" if report.direction == Direction.IN else "⬅️" if report.direction == Direction.OUT else "❓"
        print(f"   Направление:      {dir_icon} {report.final_decision}")
        print(f"   Уверенность:      {report.confidence_percent}%")
        print(f"   Консенсус:        {report.consensus.value}")
        print(f"   Транспорт:        {report.transport_mode.value}")
        
        print(f"\n📡 RFID ({report.rfid_reads} считываний):")
        if report.rfid_first and report.rfid_last:
            duration = (report.rfid_last - report.rfid_first).total_seconds()
            print(f"   Первое:         {report.rfid_first.strftime('%H:%M:%S.%f')[:-3]}")
            print(f"   Последнее:      {report.rfid_last.strftime('%H:%M:%S.%f')[:-3]}")
            print(f"   Длительность:   {duration:.1f} сек")
        
        print(f"   Зоны:             {report.rfid_zones}")
        print(f"   Средний RSSI:     {report.rfid_avg_rssi} dBm")
        print(f"   Считываний по антеннам:")
        if report.rfid_antenna_counts:
            for antenna, count in sorted(report.rfid_antenna_counts.items()):
                zone = "OUTER" if antenna in Config.OUTER_ANTENNAS else "INNER" if antenna in Config.INNER_ANTENNAS else "UNKNOWN"
                print(f"      • Антенна {antenna} ({zone}): {count} раз(а)")
        else:
            print(f"      • Нет данных")
        
        print(f"\n📹 ВИДЕО ({report.video_count} событий):")
        status = "✅" if report.video_found else "❌"
        print(f"   {status} Найдено событий: {report.video_count}")
        if report.video_found:
            print(f"   Направление:      {report.video_direction} ({self._format_direction_explanation(report.video_direction)})")
            print(f"   Транспорт:        {report.video_transport}")
            print(f"   Детали событий:")
            for i, event in enumerate(report.video_events[:5], 1):
                time_str = event['time'].strftime('%H:%M:%S.%f')[:-3] if event['time'] else 'N/A'
                print(f"      {i}. [{time_str}] {event['direction']} | {event['transport']} | Камера {event['from_cam']}→{event['to_cam']}")
            if len(report.video_events) > 5:
                print(f"      ... и еще {len(report.video_events) - 5} событий")
        
        print(f"\n🚪 СКУД ({report.skud_count} событий):")
        status = "✅" if report.skud_found else "❌"
        print(f"   {status} Найдено событий: {report.skud_count}")
        if report.skud_found:
            print(f"   Направление:      {report.skud_direction} ({self._format_direction_explanation(report.skud_direction)})")
            print(f"   Ворота:           {report.skud_gate}")
            print(f"   Лицо/Авто:        {report.skud_person}")
            print(f"   Детали событий:")
            for i, event in enumerate(report.skud_events[:5], 1):
                time_str = event['time'].strftime('%H:%M:%S.%f')[:-3] if event['time'] else 'N/A'
                print(f"      {i}. [{time_str}] {event['direction']} | {event['person']} | {event['gate']}")
            if len(report.skud_events) > 5:
                print(f"      ... и еще {len(report.skud_events) - 5} событий")
        
        if report.warnings:
            print(f"\n⚠️ ПРЕДУПРЕЖДЕНИЯ:")
            for w in report.warnings:
                print(f"   {w}")
        
        status_icon = "✅" if report.consensus in [ConsensusResult.UNANIMOUS, ConsensusResult.MAJORITY] else "⚠️"
        print(f"\n{status_icon} СТАТУС: {report.consensus.value}")
        print("═"*100)
    
    def _log(self, message: str, level: str = "INFO"):
        """Логирование"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        prefix = {
            "INFO": "ℹ️",
            "WARN": "⚠️",
            "ERROR": "❌",
            "SUCCESS": "✅"
        }.get(level, "ℹ️")
        print(f"[{timestamp}] {prefix} {message}")
        sys.stdout.flush()
    
    def run(self):
        """Основной цикл мониторинга"""
        print("\n" + "█"*100)
        print("🚀 УМНЫЙ КПП - МОНИТОРИНГ В РЕАЛЬНОМ ВРЕМЕНИ (v5.1)")
        print("█"*100)
        print(f"⏰ Запуск: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📊 Таймаут исчезновения: {Config.RFID_DISAPPEAR_TIMEOUT} сек")
        print(f"📊 Интервал опроса: {Config.POLL_INTERVAL} сек")
        print(f"📊 Обновление задач 1С: каждые {Config.TASKS_RELOAD_INTERVAL} сек")
        print(f"📊 Окно задач 1С: 48 часов (БЕЗ ЛИМИТОВ)")
        print("█"*100)
        
        tasks_count = self.load_1c_tasks()
        self.last_tasks_reload = datetime.now()
        
        if tasks_count == 0:
            self._log("⚠️ НЕТ ЗАДАНИЙ ИЗ 1С! Продолжаем мониторинг...", "WARN")
        
        self._log("🟢 НАЧАЛО МОНИТОРИНГА... (Ctrl+C для остановки)", "SUCCESS")
        print("\n")
        
        cycle_count = 0
        try:
            while True:
                cycle_count += 1
                
                # 1. ПЕРИОДИЧЕСКАЯ ПЕРЕЗАГРУЗКА ЗАДАЧ ИЗ 1С
                if (datetime.now() - self.last_tasks_reload).total_seconds() > Config.TASKS_RELOAD_INTERVAL:
                    self.load_1c_tasks()
                    self.last_tasks_reload = datetime.now()
                
                # 2. Получаем новые RFID считывания
                new_reads = self.get_new_rfid_reads()
                
                for read in new_reads:
                    self.process_rfid_read(read)
                
                # 3. Проверяем завершенные сессии
                completed = self.check_completed_sessions()
                
                for session in completed:
                    report = self.analyze_session(session)
                    self.print_report(report)
                    self.completed_reports.append(report)
                
                # 4. Статус каждые 30 секунд
                if cycle_count % 6 == 0:
                    active_count = len(self.active_sessions)
                    completed_count = len(self.completed_reports)
                    tasks_count = len(self.tasks)
                    print(f"\n[СТАТУС] Задач 1С: {tasks_count} | Активных сессий: {active_count} | Завершено: {completed_count} | RFID ID: {self.last_rfid_id}")
                
                # 5. Ждем перед следующим опросом
                time.sleep(Config.POLL_INTERVAL)
                
        except KeyboardInterrupt:
            print("\n\n⏹️  Остановка по запросу пользователя...")
        except Exception as e:
            self._log(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}", "ERROR")
            import traceback
            traceback.print_exc()
        
        # Итоговый отчет
        print("\n" + "█"*100)
        print("📊 ИТОГИ СЕССИИ")
        print("█"*100)
        print(f"⏰ Работа: {(datetime.now() - self.start_time).total_seconds()/60:.1f} мин")
        print(f"📦 Завершено пересечений: {len(self.completed_reports)}")
        
        if self.completed_reports:
            in_count = sum(1 for r in self.completed_reports if r.direction == Direction.IN)
            out_count = sum(1 for r in self.completed_reports if r.direction == Direction.OUT)
            confirmed = sum(1 for r in self.completed_reports if r.consensus in [ConsensusResult.UNANIMOUS, ConsensusResult.MAJORITY])
            
            print(f"   Въездов: {in_count}")
            print(f"   Выездов: {out_count}")
            print(f"   Подтверждено: {confirmed}/{len(self.completed_reports)}")
        
        print("█"*100)

# ============================================================================
# ЗАПУСК
# ============================================================================

if __name__ == "__main__":
    monitor = KPPMonitor()
    monitor.run()