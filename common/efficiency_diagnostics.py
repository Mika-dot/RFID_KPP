"""Evidence-backed reconciliation; absence in SQL is not a physical diagnosis."""
from collections import Counter
from datetime import datetime, timedelta

from common.efficiency_report import local_time, tag_key, percent


REASONS = {
    'READ': 'Точная метка найдена в сырых чтениях периода',
    'READ_UNCLASSIFIED': 'Сырое чтение есть; распознавание события как катушки не подтверждено',
    'NO_TAG': 'В 1С нет зарегистрированной метки на дату приёмки',
    'AMBIGUOUS': 'Метка зарегистрирована за несколькими сериями',
    'OUTSIDE_PERIOD': 'Точная метка встречается только вне выбранного периода',
    'EPC_ONLY': 'В периоде встречается EPC, но полный EPC+TID не подтверждён',
    'NOT_IN_HISTORY': 'Точная метка не найдена в доступной истории журнала антенны',
    'NOT_CHECKED': 'В периоде чтения нет; проверка остальной истории не завершена',
}


def reconcile(report, history_rows=None, *, processing=None):
    report = dict(report)
    history = {}
    start = datetime.fromisoformat(report['date_from'])
    end = datetime.fromisoformat(report['date_to'])+timedelta(days=1)
    wanted = {tag for row in report['details'] if row['has_rfid'] and not row['detected']
              for tag in set(row['registered_tags'])-set(row['ambiguous_tags'])}
    for item in history_rows or ():
        tag = tag_key(item['Tag'])
        if tag not in wanted:
            continue
        row = dict(item)
        row['ReadCount'] = int(row.get('ReadCount') or 0)
        row['EpcPeriodReads'] = int(row.get('EpcPeriodReads') or 0)
        if int(row.get('PeriodReads') or 0) > 0:
            # A newly delivered read can appear between the period and history
            # queries. Never publish the old, now contradicted numerator.
            raise ValueError('Данные RFID изменились во время формирования. Сформируйте отчёт повторно.')
        for name in ('FirstRead', 'LastRead'):
            row[name] = local_time(row[name]).isoformat() if row.get(name) else None
        if row['ReadCount']:
            if not row['FirstRead'] or not row['LastRead'] or row['FirstRead'] > row['LastRead']:
                raise ValueError('Некорректное подтверждение истории RFID')
            if start <= local_time(row['FirstRead']) < end or start <= local_time(row['LastRead']) < end:
                raise ValueError('Данные RFID изменились во время формирования. Сформируйте отчёт повторно.')
        history[tag] = row
    details, counts = [], Counter()
    for original in report['details']:
        row = dict(original)
        tags = set(row['registered_tags'])-set(row['ambiguous_tags'])
        proofs = [history[tag] for tag in sorted(tags) if tag in history]
        if row['detected']:
            recognized = bool(set(row['raw_matched_tags']) & set(row.get('aggregator_tags', ())))
            code = 'READ_UNCLASSIFIED' if report.get('events_checked', True) and not recognized else 'READ'
        elif not row['has_rfid']:
            code = 'NO_TAG'
        elif not tags:
            code = 'AMBIGUOUS'
        elif any(p['ReadCount'] for p in proofs):
            code = 'OUTSIDE_PERIOD'
        elif any(p['EpcPeriodReads'] for p in proofs):
            code = 'EPC_ONLY'
        elif tags <= history.keys():
            code = 'NOT_IN_HISTORY'
        else:
            code = 'NOT_CHECKED'
        row.update(reason_code=code, reason=REASONS[code], history_evidence=proofs)
        details.append(row)
        if row['has_rfid'] and not row['detected']:
            counts[code] += 1
    report['details'] = details
    report['reconciliation'] = dict(counts=sorted(counts.items()), history_checked=history_rows is not None and wanted <= history.keys(),
                                    processing=processing or dict(checked=False),
                                    unrecognized_series=[row['series'] for row in details if row['reason_code']=='READ_UNCLASSIFIED'])
    return report


def reconciliation_rows(report):
    return [[REASONS[code], count] for code, count in report.get('reconciliation', {}).get('counts', ())]


def verification_text(report):
    data = report.get('reconciliation', {})
    history = 'проверена' if data.get('history_checked') else 'не проверена полностью'
    logs = data.get('processing', {})
    errors = (f'записей {logs["total"]}, незакрытых {logs["unresolved"]}'
              if logs.get('checked') else 'не проверен')
    return (f'История сырых чтений: {history}. Журнал ошибок обработки: {errors}. '
            'Отсутствие записи не устанавливает физическую причину: экранирование, наличие метки и маршрут '
            'перемещения по этим SQL-таблицам не доказаны. Ошибки обработки сами по себе не объясняют '
            'отсутствие исходного чтения. Файловые журналы Reader/Guardian в этот отчёт не загружаются.')


def operational_rows(report):
    data = report.get('reconciliation', {}).get('processing', {})
    coverage = report.get('processing_coverage', {})
    received, recognized = coverage.get('received', 0), coverage.get('recognized', 0)
    software = ([f'{recognized}/{received} катушек', percent(recognized, received)]
                if report.get('events_checked', True) else ['События обработки не проверены', 'Не измерено'])
    cursor = ([f'{data["consumed"]}/{data["read_total"]} сырых строк', percent(data['consumed'], data['read_total'])]
              if data.get('pipeline_checked') else ['Нет подтверждённого курсора обработки', 'Не измерено'])
    errors = f'{data["total"]}; незакрытых {data["unresolved"]}' if data.get('checked') else 'Журнал не проверен'
    return [['Распознавание полученных RFID Периметром', *software],
            ['Прохождение входящего RFID-потока по курсору', *cursor],
            ['Зарегистрированные ошибки обработки', errors, 'Не является доказательством отсутствия всех багов'],
            ['Коммуникации / сеть', 'История сетевой доступности и потерь не предоставлена', 'Доля потерь не установлена'],
            ['Данные 1С / человеческий фактор', f'{report["totals"]["without_rfid"]} катушек без привязки метки на дату приёмки',
             'Вина и физическое наличие метки требуют проверки']]


def history_text(row):
    items = []
    for proof in row.get('history_evidence', ()):
        if proof['ReadCount']:
            items.append(f'{proof["Tag"]}: {proof["ReadCount"]} чтений; {proof["FirstRead"]}…{proof["LastRead"]}; '
                         f'ID {proof["FirstRawId"]}…{proof["LastRawId"]}')
        else:
            items.append(f'{proof["Tag"]}: точных чтений в истории нет; совпадений EPC в периоде {proof["EpcPeriodReads"]}')
    return '\n'.join(items) or 'Не проверялась / не требуется'
