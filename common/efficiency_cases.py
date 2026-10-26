"""Per-drum chronology and bounded source evidence, without inferred blame.

RfidTags.Dt is a document/registration date, NOT its arrival time in SQL.
An EPC belonging to a different TID is NOT evidence of a defective target tag.
Other antenna reads are context, NOT proof that this drum traversed the gate.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import re

from common.efficiency_report import local_time, tag_key, percent


CATEGORY_NAMES = {
    'NO_TAG': 'Нет привязки 1С',
    'OUTSIDE_PERIOD': 'Чтения вне периода',
    'EPC_ONLY': 'Совпадает только EPC',
    'NOT_IN_HISTORY': 'Нет точного чтения',
    'NOT_CHECKED': 'История не проверена',
    'AMBIGUOUS': 'Неоднозначная метка',
    'READ_UNCLASSIFIED': 'Не распознана катушка',
}


def activity_window(warehouse_dt):
    """The receipt's hour, 12 whole hours before it and 12 after it.

    SQL evidence is hourly: use the same 25 buckets in facts and graphs.
    The end is exclusive; a partial receipt hour never shifts one view alone.
    """
    anchor = local_time(warehouse_dt).replace(minute=0, second=0, microsecond=0)
    return anchor-timedelta(hours=12), anchor+timedelta(hours=13)


def needs_review(row):
    return not row['has_rfid'] or not row['detected'] or row.get('reason_code') == 'READ_UNCLASSIFIED'


def case_tags(report):
    return {tag_key(item['tag']) for row in report['details'] if needs_review(row)
            for item in row.get('registrations', ())} | {
                tag for row in report['details'] if needs_review(row) for tag in row['registered_tags']}


def normal_bin(source):
    row = dict(source)
    row['EPC'], row['TID'] = tag_key(row.get('EPC')), tag_key(row.get('TID'))
    row['Tag'] = row['EPC'] + row['TID']
    for name in ('FirstRead', 'LastRead', 'FirstReceived', 'LastReceived'):
        row[name] = local_time(row[name]).isoformat() if row.get(name) else None
    row['ReadCount'] = int(row.get('ReadCount') or 0)
    row['LateRows'] = int(row.get('LateRows') or 0)
    row['TimedRows'] = int(row.get('TimedRows') or 0)
    if row['FirstRead'] and row['LastRead'] and (row['FirstRead'] > row['LastRead'] or row['FirstRead'][:10] != row['LastRead'][:10]):
        raise ValueError('Некорректный дневной интервал детализации RFID')
    return row


def attach_case_analysis(report, raw_bins=None, activity=None, event_rows=None):
    """Attach inspectable facts; optional-source failure remains unmeasured."""
    report = dict(report)
    bins = [normal_bin(row) for row in raw_bins or ()]
    by_epc, events_by_tag = defaultdict(list), defaultdict(list)
    for row in bins:
        if row['ReadCount'] <= 0 or not row['FirstRead'] or not row['LastRead']:
            raise ValueError('Некорректная детализация журнала RFID')
        by_epc[row['EPC']].append(row)
    if raw_bins is not None:
        # This later, more detailed all-history query can complete an earlier
        # optional history check. Never leave "unchecked" beside verified bins.
        # Only canonical EPC24+TID24 identities were actually queried.
        from common.efficiency_diagnostics import reconcile
        start = datetime.fromisoformat(report['date_from'])
        end = datetime.fromisoformat(report['date_to']) + timedelta(days=1)
        history = {p['Tag']: p for d in report['details'] for p in d.get('history_evidence', ())}
        for d in report['details']:
            if d['detected'] or not d['has_rfid']:
                continue
            for tag in set(d['registered_tags'])-set(d['ambiguous_tags']):
                if not re.fullmatch('[0-9A-F]{48}', tag):
                    continue
                group = by_epc.get(tag[:24], ())
                exact = [p for p in group if p['Tag'] == tag]
                in_period = lambda p: start <= local_time(p['FirstRead']) < end
                history[tag] = dict(Tag=tag,ReadCount=sum(p['ReadCount'] for p in exact),
                                    PeriodReads=sum(p['ReadCount'] for p in exact if in_period(p)),
                                    EpcPeriodReads=sum(p['ReadCount'] for p in group if in_period(p)),
                                    FirstRead=min((p['FirstRead'] for p in exact),default=None),
                                    LastRead=max((p['LastRead'] for p in exact),default=None),
                                    FirstRawId=min((int(p['FirstRawId']) for p in exact),default=None),
                                    LastRawId=max((int(p['LastRawId']) for p in exact),default=None))
        report = reconcile(report,list(history.values()),processing=report.get('reconciliation',{}).get('processing'))
    for source in event_rows or ():
        if int(source.get('RfidReadCount') or 0) <= 0:
            continue
        if str(source.get('SessionCloseReason') or '').upper() in {'WAREHOUSE_ONLY', 'SUPERSEDED_BY_KPP'}:
            continue
        row = dict(source)
        tag = tag_key(row.get('SourceTag')) or tag_key(row.get('EPC')) + tag_key(row.get('TID'))
        for key in ('FirstSeen', 'LastSeen', 'Task1CDt', 'CreatedAt', 'CompletedAt'):
            row[key] = local_time(row[key]).isoformat() if row.get(key) else None
        if tag:
            events_by_tag[tag].append(row)
    activities = []
    for source in activity or ():
        row = dict(source)
        row['Hour'] = local_time(row['Hour']).isoformat()
        row['ReadCount'] = int(row['ReadCount'])
        activities.append(row)
    report['case_sources'] = dict(raw_checked=raw_bins is not None,
                                  activity_checked=activity is not None, events_checked=event_rows is not None)
    report['activity'] = sorted(activities, key=lambda row: (row['Hour'], str(row.get('Antenna'))))
    start = datetime.fromisoformat(report['date_from'])
    end = datetime.fromisoformat(report['date_to']) + timedelta(days=1)
    details = []
    for original in report['details']:
        row = dict(original)
        regs = row.get('registrations', ())
        tags = set(row['registered_tags']) | {item['tag'] for item in regs}
        epcs = {tag[:24] for tag in tags if len(tag) == 48}
        checked = raw_bins is not None and bool(tags) and all(re.fullmatch('[0-9A-F]{48}', tag) for tag in tags)
        relevant = [p for epc in sorted(epcs) for p in by_epc.get(epc, ())]
        exact = [p for p in relevant if p['Tag'] in tags]
        variants = [p for p in relevant if p['Tag'] not in tags]
        per = lambda p: start <= local_time(p['FirstRead']) < end
        # Detect a late spool delivery contradicting the earlier KPI snapshot.
        if not row['detected'] and any(per(p) and p['Tag'] in set(row['registered_tags'])-set(row['ambiguous_tags']) for p in exact):
            raise ValueError('Данные RFID изменились во время формирования. Сформируйте отчёт повторно.')
        receipt = local_time(row['warehouse_dt'])
        later = [r for r in regs if local_time(r['dt']) > receipt]
        row['binding_status'] = ('Есть к дате склада' if row['has_rfid'] else
                                 'Дата привязки позже склада' if later else 'Привязка отсутствует в выгрузке')
        row['later_registration_dates'] = sorted({r['dt'] for r in later})
        if not needs_review(row):
            details.append(row)
            continue
        own_events = {str(e['EventId']): e for tag in tags for e in events_by_tag.get(tag, ())}
        timeline = []
        def point(at, stage, source, fact):
            if at:
                timeline.append(dict(at=at, stage=stage, source=source, fact=fact))
        point(start.isoformat(), 'Начало отчёта', 'Выбранный период', '')
        point(end.isoformat(), 'Конец отчёта (не включён)', 'Выбранный период', '')
        for r in regs:
            point(r['dt'], 'Дата регистрации / документа 1С', 'RfidTags.Id='+r['id'],
                  r['tag'] + ('; документ '+r['document'] if r['document'] else ''))
        for w in row.get('warehouse_receipts', ()):
            point(w['dt'], 'Запись адресного склада', 'Warehouse.Id='+w['id'], row['series'])
        for p in exact:
            scope = 'в периоде' if per(p) else 'до периода' if local_time(p['LastRead']) < start else 'после периода'
            identity = f'RFID_Tags.Id {p["FirstRawId"]}…{p["LastRawId"]}'
            point(p['FirstRead'], 'Точное чтение '+scope, identity,
                  f'{p["Tag"]}; антенна {p.get("Antenna", "—")}; чтений {p["ReadCount"]}')
            if p['LastRead'] != p['FirstRead']:
                point(p['LastRead'], 'Последнее чтение в группе', identity, p['Tag'])
            point(p.get('FirstReceived'), 'Первая доставка группы в SQL', identity,
                  'MIN(ReceivedAt) группы; не обязательно строка первого чтения')
        for e in own_events.values():
            point(e.get('FirstSeen'), 'Физическое событие Периметра', 'KPP_ReelEvents.EventId='+str(e['EventId']),
                  f'IsReel={e.get("IsReel", "—")}; направление={e.get("FinalDirection") or "—"}; '
                  f'классификация={e.get("ReelClassification") or "—"}; Task1CId={e.get("Task1CId") or "—"}')
        before = [p for p in exact if local_time(p['LastRead']) < start]
        after = [p for p in exact if local_time(p['FirstRead']) >= end]
        empty_tid = sum(p['ReadCount'] for p in variants if per(p) and not p['TID'])
        other_tid = sum(p['ReadCount'] for p in variants if per(p) and p['TID'])
        late_docs = []
        for tag in sorted(tags):
            dates = [r['dt'] for r in regs if r['tag'] == tag]
            reads = [p['FirstRead'] for p in exact if p['Tag'] == tag]
            if dates and reads and min(reads) < min(dates):
                late_docs.append(dict(tag=tag, read=min(reads), document=min(dates)))
        context_start, context_end = activity_window(receipt)
        hourly = [p for p in report['activity']
                  if context_start <= local_time(p['Hour']) < context_end]
        facts = [
            ['Привязка 1С', row['binding_status'], 'RfidTags.Dt и Warehouse.Dt'],
            ['Точное чтение в отчётном периоде',
             str(sum(p['ReadCount'] for p in exact if per(p))) if checked else
             str(sum(p['read_count'] for p in row['raw_evidence'])) if row['has_rfid'] else 'Нет идентификатора для проверки',
             'Полный EPC+TID'],
            ['Точное чтение до / после периода', f'{sum(p["ReadCount"] for p in before)} / {sum(p["ReadCount"] for p in after)}'
             if checked else 'Не проверено полностью', 'История RFID_Tags'],
            ['Тот же EPC, TID отсутствует', empty_tid if checked else 'Не проверено', 'Число строк периода'],
            ['Тот же EPC, другой TID', other_tid if checked else 'Не проверено', 'Число строк периода; другая полная метка'],
            ['Дата документа 1С позже точного чтения', len(late_docs) if checked else 'Не проверено',
             'Сравнение дат; время доставки 1С неизвестно'],
            ['Другие чтения: 25 часов вокруг часа склада', sum(p['ReadCount'] for p in hourly)
             if activity is not None else 'Не проверено',
             f'Контекст {context_start:%d.%m %H:%M}–{context_end:%d.%m %H:%M} (конец не включён); проезд не доказан'],
        ]
        checks = diagnosis_rows(row, checked, before, after, empty_tid, other_tid, late_docs, event_rows is not None)
        row['case'] = dict(facts=facts, checks=checks, timeline=sorted(timeline, key=lambda p: (p['at'], p['stage'], p['source'])),
                           raw_history=sorted(relevant, key=lambda p: (p['FirstRead'], p['Tag'], str(p.get('Antenna')))),
                           late_documents=late_docs, raw_checked=checked,
                           events=sorted(own_events.values(), key=lambda e: (e['FirstSeen'] or '', str(e['EventId']))))
        details.append(row)
    report['details'] = details
    report['one_c'] = dict(without_binding=sum(not r['has_rfid'] for r in details),
                           later_binding=sum(not r['has_rfid'] and bool(r['later_registration_dates']) for r in details),
                           no_binding_in_export=sum(not r['has_rfid'] and not r['later_registration_dates'] for r in details),
                           document_after_read=sum(bool(r.get('case', {}).get('late_documents')) for r in details),
                           arrival_checked=False)
    return report


def diagnosis_rows(row, checked, before, after, empty_tid, other_tid, late_docs, events_checked):
    code = row['reason_code']
    checks = []
    def add(topic, status, proof, required):
        checks.append([topic, status, proof, required])
    if code == 'NO_TAG':
        add('Данные 1С', 'Установлено', row['binding_status'], 'Проверить привязку и исходящий журнал 1С')
    elif code == 'AMBIGUOUS':
        add('Данные 1С', 'Установлено', 'Одна полная метка связана с несколькими сериями', 'Сверить актуальную принадлежность метки')
    elif code == 'READ_UNCLASSIFIED':
        add('Обработка ПО', 'Расхождение установлено', 'Точное чтение есть; IsReel=1 не подтверждён',
            'Проверить классификацию и доступный реестр в момент события')
    elif code == 'OUTSIDE_PERIOD':
        add('Период движения', 'Установлены даты чтений' if checked else 'Часть истории проверена',
            f'Групп до периода: {len(before)}; после: {len(after)}' if checked else 'См. проверенные границы истории в сверке',
            'Сопоставить документы с конкретным перемещением; старое чтение не доказывает новый проезд')
    elif code == 'EPC_ONLY':
        add('Идентичность метки', 'Совпадение EPC установлено',
            f'Без TID: {empty_tid}; с другим TID: {other_tid}' if checked else 'Полная детализация не проверена',
            'Считать метку вручную и сверить EPC+TID; другой TID может принадлежать другой катушке')
    else:
        add('Исходное чтение', 'Нет подтверждения', row['reason'], 'Проверить физическую метку и маршрут перемещения')
    if late_docs:
        add('Порядок дат 1С', 'Установлено по датам', 'Дата регистрации/документа позже точного чтения',
            'Для вывода о поздней передаче нужен журнал получения 1С сервером')
    add('Передача 1С', 'Не измерена', 'RfidTags не содержит времени поступления записи', 'Журнал обмена 1С с датой отправки и получения')
    add('Внешняя сеть', 'Причина не установлена', 'ReceivedAt измеряет общую доставку; истории доступности нет',
        'Инцидент сети/оборудования с временем и привязкой к подтверждённому проезду')
    add('Антенна / физическая метка', 'Причина не установлена',
        'Чужие чтения не доказывают исправность всех антенн; отсутствие TID не доказывает дефект метки',
        'Диагностика считывателя по портам, проверка наклейки, видео/журнал проезда')
    if not events_checked:
        add('События Периметра', 'Не проверены полностью', 'История событий недоступна', 'Повторить формирование после восстановления источника')
    return checks


def short_scorecard(report):
    """Metric, numerator, denominator, rate/status, concise scope."""
    t = report['totals']
    p = report.get('processing_coverage', {})
    d = report.get('delivery', {})
    one = report.get('one_c', {})
    rows = [
        ['Периметр целиком', t['perimeter'], t['total'], percent(t['perimeter'], t['total']), 'RFID КПП + адресный склад'],
        ['RFID-блок: считывание меток 1С', t['detected'], t['tagged'],
         percent(t['detected'], t['tagged']) if report.get('raw_checked') else 'Не проверено', 'Катушки с точным чтением / с меткой 1С к приёмке'],
        ['Автоматическая обработка поступивших RFID', p.get('recognized', 0) if report.get('events_checked', True) else '—', p.get('received', 0),
         percent(p.get('recognized', 0), p.get('received', 0)) if report.get('events_checked', True) else 'Не проверено',
         'Распознано катушек / катушки с сырым чтением'],
        ['Отказы внешней инфраструктуры', '—', '—', 'Не установлено', 'Нет истории инцидентов с привязкой к проездам'],
    ]
    if d.get('checked'):
        rows.append([f'Задержки доставки Reader → SQL > {d["threshold_sec"]} с', d['late_rows'], d['timed_rows'],
                     percent(d['late_rows'], d['timed_rows']), 'Строки; причина задержки не установлена'])
    else:
        rows.append(['Задержки доставки Reader → SQL', '—', '—', 'Не измерено', 'Нет пригодных временных меток'])
    rows.extend([
        ['1С: нет привязки к дате склада', t['without_rfid'], t['total'], percent(t['without_rfid'], t['total']),
         'Учтены складом; исключены из знаменателя RFID'],
        ['1С: дата привязки позже склада', one.get('later_binding', 'Не проверено'), '—', 'Факт дат', 'Подмножество катушек без привязки к приёмке'],
        ['1С: задержка передачи / человеческая ошибка', '—', '—', 'Не установлено', 'Нет журнала обмена и результатов проверки'],
    ])
    return rows


def issue_counts(report):
    return Counter(row['reason_code'] for row in report['details'] if needs_review(row))
