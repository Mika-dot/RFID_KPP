"""Reader-facing tables and individual investigation sheets for report v1.4."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import re

from common.efficiency_report import HEADERS, methodology_text, period_text, summary_rows, percent
from common.efficiency_diagnostics import history_text
from common.efficiency_cases import CATEGORY_NAMES, issue_counts, needs_review, short_scorecard, activity_window


def link(name, text='Открыть разбор'):
    return dict(link="'"+name.replace("'", "''")+"'!A1", text=text)


def case_names(report):
    names = {}
    for row in report['details']:
        if needs_review(row):
            short = re.sub(r'[\\/*?:\[\]\x00-\x1f\x7f]', '-', row['series']).strip("'")
            names[row['series']] = f'{len(names)+1:03d}_'+short[:27]
    return names


def main_sheet(report, Sheet):
    sheet = Sheet([27, 20, 12, 12, 19, 49])
    sheet.one_page = True
    sheet.text('КПП «ПЕРИМЕТР». ЭФФЕКТИВНОСТЬ', 1)
    sheet.text('Период: '+period_text(report)+' (МСК)')
    sheet.row(height=8)
    n = sheet.row(['Показатель', '', 'Учтено', 'База', 'Результат', 'Что измерено'], 2, 30)
    sheet.merges.append(f'A{n}:B{n}')
    for index, (name, numerator, denominator, rate, scope) in enumerate(short_scorecard(report)):
        number = len(sheet.rows)+1
        rate_value = rate
        # Counts may exist even when an optional measurement was unavailable.
        # Preserve that status instead of manufacturing a numeric percentage.
        if (isinstance(numerator, int) and isinstance(denominator, int)
                and rate == percent(numerator, denominator)):
            rate_value = ({'formula': f'IF(D{number}=0,"",C{number}/D{number})',
                           'value': numerator/denominator if denominator else ''}, 4 if index < 2 else 3)
        n = sheet.row([name, '', numerator, denominator, rate_value, scope], 2 if index < 2 else 0, 36)
        sheet.merges.append(f'A{n}:B{n}')
    sheet.row(height=8)
    sheet.text('КОНТРОЛЬ И РАСХОЖДЕНИЯ', 2)
    p = report.get('reconciliation', {}).get('processing', {})
    checks = [
        ['Нет входного чтения (разбор)', report.get('accountability',{}).get('unresolved_no_input','Не проверено'), 'Катушки без распознавания',
         len(report.get('reconciliation', {}).get('unrecognized_series', ())) if report.get('events_checked',True) else 'Не проверено'],
        ['Обработано входящих строк', f'{p["consumed"]}/{p["read_total"]}' if p.get('pipeline_checked') else 'Не проверено',
         'Ошибки обработки / незакрытые', f'{p["total"]} / {p["unresolved"]}' if p.get('checked') else 'Не проверено'],
    ]
    for a, b, c, d in checks:
        n = sheet.row([a, '', b, c, '', d], height=32)
        sheet.merges.extend([f'A{n}:B{n}', f'D{n}:E{n}'])
    sheet.text('RFID KPI оценивает обработку полученных чтений при имеющейся распорядке. Причины непоступления данных — отдельно.', 0)
    return sheet


def daily_sheet(report, Sheet):
    sheet = Sheet([18, 23, 18, 22, 18, 32, 15, 15])
    sheet.text('ДИНАМИКА ЗА ПЕРИОД', 1)
    sheet.text(period_text(report)+' (МСК)')
    chart_row = sheet.reserve_chart()
    header = sheet.row(HEADERS, 2, 36)
    display_rows=summary_rows(report)
    checked=report.get('raw_checked') and report.get('events_checked',True)
    for display in display_rows[:-1]:
        n = len(sheet.rows)+1
        sheet.row([display[0], display[1], display[2], display[3],
                   ({'formula': f'IF(C{n}=0,"",D{n}/C{n})', 'value':display[3]/display[2] if display[2] else ''},3) if checked else 'Не проверено',
                   display[5]])
    last, n = len(sheet.rows), len(sheet.rows)+1
    total=display_rows[-1]
    sheet.row(['Итого', *[({'formula':f'SUM({col}{header+1}:{col}{last})','value':total[index]},8)
                              for col,index in [('B',1),('C',2)]],
               ({'formula':f'SUM(D{header+1}:D{last})','value':total[3]},8) if checked else 'Не проверено',
               ({'formula': f'IF(C{n}=0,"",D{n}/C{n})','value':total[3]/total[2] if total[2] else ''},4) if checked else 'Не проверено',
               total[5]],2)
    sheet.chart('Учёт Периметра и входные данные КПП (катушки)',header+1,last,[2,3,4] if checked else [2,3],chart_row)
    return sheet


def reconciliation_sheet(report, Sheet, names):
    headers = ['Серия', 'Дата склада (МСК)', 'ID склада', 'С RFID', 'Считано', 'Периметр',
               'Полные метки распорядок 1С', 'Полные метки с чтением', 'ID связи с приёмкой', 'Неоднозначные метки',
               'Reader подтвердил', 'Связь с приёмкой подтверждена', 'Доказательства Reader (границы групп)',
               'Чтений меток', 'Метки без подтверждения', 'Установлено по данным', 'История чтений вне периода',
               'Распознано как катушка', 'ID распознанных событий', 'ID всех физических событий периода',
               'Время передачи распорядок 1С', 'Результат контроля КПП', 'Передачи 1С позже склада (контекст)', 'Разбор']
    sheet = Sheet([18,24,22,12,12,14,60,60,22,50,16,18,85,14,60,55,85,18,25,25,35,36,35,25])
    sheet.row(headers,2,50)
    sheet.freeze=1
    for row in report['details']:
        control=row['control'];recognized=control['recognized']
        all_proofs=row.get('all_raw_evidence',row['raw_evidence'])
        sheet.row([row['series'], datetime.fromisoformat(row['warehouse_dt']), '; '.join(row['warehouse_ids']),
                   int(bool(row['registrations'])),int(bool(all_proofs)),int(row['in_perimeter']),
                   '\n'.join(sorted({r['tag'] for r in row['registrations']})), '\n'.join(sorted({p['tag'] for p in all_proofs})), '; '.join(row['event_ids']),
                   '\n'.join(row['ambiguous_tags']),int(row['raw_detected']),
                   int(row['kpp_detected']) if report.get('events_checked',True) else 'Не проверено',
                   '\n'.join(f'{p["first_id"]}…{p["last_id"]}; {p["first_read"]}…{p["last_read"]}; {p["tag"]}' for p in all_proofs),
                   sum(p['read_count'] for p in all_proofs), '\n'.join(row['unread_tags']), row['reason'], history_text(row),
                   int(recognized) if report.get('events_checked',True) else 'Не проверено',
                   '; '.join(row.get('aggregator_event_ids',())), '; '.join(row.get('observed_event_ids',())),
                   '\n'.join(sorted({r['dt'] for r in row['registrations']})), control['result'],
                   '\n'.join(row.get('later_registration_dates',())),
                   link(names[row['series']]) if row['series'] in names else 'Чтение и распознавание подтверждены'],
                  height=min(400,max(55,28*max(len(row['registered_tags']),1))))
    sheet.filter=f'A1:X{len(sheet.rows)}'
    return sheet


def review_sheet(report, Sheet, names):
    sheet=Sheet([18,24,23,27,60,35,50,25,23,65,25])
    sheet.row(['Серия','Дата склада','ID склада','Категория','Установлено по данным',
               'Причина (заполняется)','Документ / журнал / фото','Ответственный','Дата проверки','Комментарий','Разбор'],2,50)
    sheet.freeze=1
    for row in report['details']:
        if needs_review(row):
            sheet.row([row['series'],datetime.fromisoformat(row['warehouse_dt']),'; '.join(row['warehouse_ids']),
                       CATEGORY_NAMES[row['reason_code']],row['reason'],('Не проверено',10),('',10),('',10),('',10),('',10),
                       link(names[row['series']])],height=65)
    last=len(sheet.rows)
    sheet.filter=f'A1:K{last}'
    if last>1:
        sheet.validations=(f'<dataValidations count="1"><dataValidation type="list" allowBlank="1" showErrorMessage="1" sqref="F2:F{last}">'
                           '<formula1>"Не проверено,Данные 1С,Сеть,Оборудование,ПО,Физическая метка,Маршрут,Другое"</formula1>'
                           '</dataValidation></dataValidations>')
    sheet.text('Жёлтые поля заполняются по проверке. RfidTags.Dt — момент передачи распорядки 1С согласно подтверждённому правилу обмена.')
    return sheet


def category_sheet(report, code, Sheet, names):
    rows=[r for r in report['details'] if r['reason_code']==code]
    sheet=Sheet([18,20,20,20,20,20,20,20])
    sheet.text(CATEGORY_NAMES[code].upper(),1)
    sheet.text(period_text(report)+f' (МСК). Катушек: {len(rows)}')
    explanations={
        'NO_TAG':'В доступном реестре 1С нет распорядки с полной меткой катушки. Задание на контроль КПП не подтверждено.',
        'LATE_REQUEST':'Сначала чтение или складская запись, затем распорядка 1С. Точная последовательность и ID — в карточках.',
        'OUTSIDE_PERIOD':'Есть точные чтения в другие даты. Они не подтверждают проезд в выбранном периоде.',
        'EPC_ONLY':'EPC совпадает, но полного EPC+TID нет. Пустой TID и другой TID показаны отдельно в карточках.',
        'NOT_IN_HISTORY':'Полное совпадение не найдено в доступной истории SQL. Причину определяют по журналу проезда и диагностике.',
        'NOT_CHECKED':'Чтения периода проверены. Проверка остальной истории недоступна или формат метки не поддержан.',
        'AMBIGUOUS':'Полная метка назначена разным сериям. Принадлежность требует проверки 1С.',
        'READ_UNCLASSIFIED':'Чтение поступило, но физическое событие IsReel=1 не подтверждено. Проверьте классификацию.',
        'PROCESSING_UNCHECKED':'Чтение и распорядка есть. Источник событий обработки не проверен; это не 0% распознавания.',
    }
    sheet.text(explanations[code])
    chart_row=sheet.reserve_chart()
    sheet.row(['Дата склада','Катушек'],2)
    first=len(sheet.rows)+1
    counts=Counter(r['warehouse_dt'][:10] for r in rows)
    for day in report['days']:
        sheet.row([datetime.fromisoformat(day['date']).strftime('%d.%m'),counts[day['date']]])
    sheet.chart('Катушки по дате склада (шт.)',first,len(sheet.rows),[2],chart_row)
    sheet.row(height=12)
    sheet.pairs(['Серия','Дата склада (МСК)','Установлено по данным','Индивидуальная карточка'],2,36)
    for row in rows:
        sheet.pairs([row['series'],datetime.fromisoformat(row['warehouse_dt']),row['reason'],link(names[row['series']])],height=60)
    return sheet


def case_sheet(report, row, Sheet, review_row):
    sheet=Sheet([18,20,20,20,20,20,20,20])
    sheet.text('КАТУШКА '+row['series'],1)
    sheet.text(period_text(report)+' (МСК). '+CATEGORY_NAMES[row['reason_code']])
    sheet.pairs([link('Проверка катушек','К списку проверки'),
                 dict(link=f"'Проверка катушек'!F{review_row}",text='Записать причину и доказательство'),
                 link('Сверка','Сводная сверка'),link('Отчёт','Краткий отчёт')],height=30)
    case=row.get('case',{})
    sheet.text('УСТАНОВЛЕНО ПО ДАННЫМ',2)
    for topic,value,source in case.get('facts',()):
        n=sheet.row([topic,'',value,'',source,'','',''],height=36)
        sheet.merges.extend([f'A{n}:B{n}',f'C{n}:D{n}',f'E{n}:H{n}'])
    if not case:
        sheet.text('Детальная история не загружена. Непроверенные причины не установлены.')
    sheet.text('ПРИЧИНЫ И ПРОВЕРКИ',2)
    sheet.pairs(['Предмет','Статус','Доказательство / граница','Что проверить'],2,30)
    for check in case.get('checks',()):
        sheet.pairs(check,height=64)
    tags=set(row['registered_tags']) | {r['tag'] for r in row.get('registrations',())}
    evidence=case.get('raw_history',())
    if tags and case.get('raw_checked'):
        sheet.text('ЧТЕНИЯ ОТЧЁТНОГО ПЕРИОДА',2)
        anchor=sheet.reserve_chart()
        header=sheet.row(['Дата','Точный EPC+TID','EPC без TID','EPC с другим TID'],2,36)
        counts=defaultdict(Counter)
        for proof in evidence:
            key='exact' if proof['Tag'] in tags else 'other' if proof['TID'] else 'empty'
            counts[proof['FirstRead'][:10]][key]+=proof['ReadCount']
        for day in report['days']:
            c=counts[day['date']]
            sheet.row([datetime.fromisoformat(day['date']).strftime('%d.%m'),c['exact'],c['empty'],c['other']])
        sheet.chart('Чтения этой метки и совпадения EPC (строки)',header+1,len(sheet.rows),[2,3,4],anchor)
    activity_anchor=row['control']['activity_anchor']
    sheet.text('РАБОТА АНТЕНН ВОКРУГ ЧТЕНИЯ' if row['control']['first_read'] else 'КОНТЕКСТ СКЛАДСКОЙ ЗАПИСИ',2)
    sheet.text('25 часовых интервалов вокруг чтения.' if row['control']['first_read'] else '25 часовых интервалов вокруг складской записи; точное время проезда неизвестно.')
    if report.get('case_sources',{}).get('activity_checked'):
        anchor=sheet.reserve_chart()
        first_hour, context_end=activity_window(activity_anchor)
        activities=report.get('activity',())
        antennas=sorted({str(p['Antenna']) for p in activities})
        # Hardware has four ports. An unexpected additional source stays in the
        # evidence table, never silently dropped to meet a drawing constraint.
        counts=Counter()
        if len(antennas)>7:
            sheet.widths.extend([20]*(len(antennas)-7))
        for p in activities:
            counts[(p['Hour'],str(p['Antenna']))]+=p['ReadCount']
        header=sheet.row(['Час МСК',*[f'Антенна {a}' for a in antennas]],2,30)
        if not antennas:
            sheet.text('В контекстном диапазоне записей нет; это не доказательство сетевого отказа.')
        else:
            for i in range(int((context_end-first_hour).total_seconds()/3600)):
                at=first_hour+timedelta(hours=i)
                sheet.row([at.strftime('%d.%m %H:%M'),*[counts[(at.isoformat(),a)] for a in antennas]])
            sheet.chart('Весь поток по антеннам (строк в час)',header+1,len(sheet.rows),list(range(2,len(antennas)+2)),anchor,kind='line')
    else:
        sheet.text('История активности антенн не проверена.')
    sheet.text('ХРОНОЛОГИЯ И ССЫЛКИ НА ЗАПИСИ',2)
    sheet.pairs(['Дата / время МСК','Событие','Источник / ID','Факт'],2,30)
    for point in case.get('timeline',()):
        sheet.pairs([datetime.fromisoformat(point['at']),point['stage'],point['source'],point['fact']],height=56)
    sheet.text('ГРУППЫ ИСТОРИИ RFID ПО ДНЯМ И АНТЕННАМ',2)
    sheet.text('MIN/MAX времени и ID — границы группы, а не обязательно одна и та же строка. Доставка — MIN/MAX ReceivedAt.')
    sheet.row(['Первое чтение','Последнее чтение','EPC','TID','Антенна / чтений','ID min / max',
               'Доставка в SQL (границы)','Позже 120 с / измерено'],2,44)
    for proof in evidence:
        sheet.row([datetime.fromisoformat(proof['FirstRead']),datetime.fromisoformat(proof['LastRead']),proof['EPC'],
                   proof['TID'] or 'TID отсутствует',f'{proof.get("Antenna", "—")} / {proof["ReadCount"]}',
                   f'{proof["FirstRawId"]} / {proof["LastRawId"]}',
                   (proof.get('FirstReceived') or '—')+'\n'+(proof.get('LastReceived') or '—'),
                   f'{proof["LateRows"]} / {proof["TimedRows"]}'],height=56)
    if not evidence:
        sheet.text('Совпадающих групп нет.' if case.get('raw_checked') else 'История не проверена / отсутствует идентификатор метки.')
    return sheet


def method_sheet(report, Sheet):
    sheet=Sheet([22,22,22,22,22,22,22,22])
    sheet.text('ИСТОЧНИКИ И МЕТОДИКА v'+report['version'],1)
    sheet.text('Сформировано: '+report['generated_at'])
    sheet.pairs(['Источник','Поле / ключ','Проверка','Ограничение'],2)
    for values in [
        ['Адресный склад','Warehouse.Id,Dt,SeriesNumber','Все серии выбранного периода','Внешний список проездов не предоставлен'],
        ['Распорядки выходного отделения / 1С','RfidTags.Id,Dt,Tag,Ids,SeriesNumber','Передача распорядки относительно чтения','Dt — момент передачи по подтверждённому правилу обмена'],
        ['Антенна','RFID_Tags.Id,EPC,TID,RecordTime','Полные метки периода; история EPC по дням/антеннам','История только в доступном SQL, не в удалённых файлах'],
        ['Периметр','KPP_ReelEvents.EventId,IsReel,FinalDirection','Распознавание и история физических событий','Направление берётся из события, не выдумывается по чтению'],
        ['Доставка','ReceivedAt,SourceReaderTime,TimeQuality','Задержки >120 с','Это не доля отказа сети'],
        ['Ошибки ПО','KPP_ProcessingErrors; RuntimeState','Ошибки и курсор обработки','Ноль ошибок не доказывает отсутствие всех багов'],
    ]:
        sheet.pairs(values,height=55)
    sheet.text(methodology_text(report))
    sheet.text('RFID KPI = распознанные катушки / катушки с полученными чтениями и распорядкой 1С. '
               'Непоступившие чтения и отсутствующие/поздние распорядки разбираются отдельно. '
               'Отсутствие исходного чтения не объявляется доказанным отказом программной обработки или доказанной внешней причиной.')
    sheet.text('Графики отражают строки SQL, а не количество физических проездов. Другой TID не доказывает повреждение целевой метки. '
               'RfidTags.Dt — момент передачи распорядки, Warehouse — последующий этап. '
               'Заявка действует до исполнения без срока; источник не содержит явного статуса закрытия. '
               'Локальный журнал Reader не является полной историей доступности всех HA-узлов.')
    return sheet


def request_sheet(report,Sheet):
    sheet=Sheet([18,23,23,36,54,24,24,23,24,24,42,25,48])
    sheet.row(['Катушка','ID распорядки RfidTags','Передано Периметру (МСК)','Документ 1С (Ids)',
               'Полная метка EPC+TID','Первое чтение периода','Последнее чтение периода',
               'Передана после первого чтения','Складская запись (контекст)','После складской записи',
               'ID исходных чтений (границы)','Источник','Ранее связанные события (контекст)'],2,60)
    sheet.freeze=1
    by_series={d['series']:d for d in report['details']}
    for r in report.get('request_evidence',()):
        own=by_series[r['series']]
        events=own.get('case',{}).get('events')
        prior=([str(e['EventId']) for e in events if str(e.get('Task1CId') or '')==r['id']]
               if events is not None else None)
        sheet.row([r['series'],r['id'],datetime.fromisoformat(r['received_at']),r['document'],r['tag'],
                   datetime.fromisoformat(r['first_read']) if r['first_read'] else 'Нет чтения периода',
                   datetime.fromisoformat(r['last_read']) if r['last_read'] else 'Нет чтения периода',
                   'Да' if r['after_first_read'] else 'Нет' if r['first_read'] else 'Не сопоставлено',
                   datetime.fromisoformat(r['warehouse_dt']),'Да' if r['after_warehouse'] else 'Нет',r['raw_ids'],
                   '1С / выходное отделение', '; '.join(prior) if prior else 'Не найдено в загруженной истории' if prior==[] else 'История не загружена'],height=60)
    for d in report['details']:
        if not d['registrations']:
            sheet.row([d['series'],'Нет распорядки','—','—','—','Не установлен идентификатор метки','—','—',
                       datetime.fromisoformat(d['warehouse_dt']),'—','—','1С / выходное отделение','—'],height=45)
    sheet.filter=f'A1:M{len(sheet.rows)}'
    sheet.text('Источник времени заявки — RfidTags.Dt (передача распорядки 1С, подтверждено владельцем обмена). '
               'Поздняя дополнительная строка не доказывает отсутствия более ранней заявки. '
               'Связанное событие само по себе не подтверждает закрытие/исполнение распорядки.')
    return sheet


def communications_sheet(report,Sheet):
    sheet=Sheet([22,22,22,22,22,22,22,22])
    sheet.text('СВЯЗЬ СО СЧИТЫВАТЕЛЕМ',1)
    c=report.get('communications',{})
    sheet.pairs(['Источник','Проверка','Область','Граница'],2)
    sheet.pairs(['Guardian: RfidReader.log и ротации','Прочитан' if c.get('checked') else 'Недоступен',
                 c.get('node','Локальный узел'),c.get('reason','Журналы не предоставлены с выгрузкой БД')],height=55)
    sheet.text('События подключения показывают наблюдения на конкретном узле. '
               'Они не равны глобальному отказу всех HA-узлов и не доказывают причину пропуска конкретной катушки.')
    sheet.pairs(['Момент МСК','Наблюдение','Источник / строка','Что установлено'],2)
    for e in c.get('events',()):
        sheet.pairs([datetime.fromisoformat(e['at']),e['kind'],e['source'],e['detail']],height=45)
    sheet.text('ИНТЕРВАЛЫ НЕУДАЧНОГО ПОДКЛЮЧЕНИЯ НА ЛОКАЛЬНОМ УЗЛЕ',2)
    sheet.pairs(['Начало','Соединение восстановлено','Длительность (с)','Доказательство'],2)
    for i in c.get('intervals',()):
        sheet.pairs([datetime.fromisoformat(i['start']),datetime.fromisoformat(i['end']),i['duration_sec'],
                     i['start_source']+'; '+i['end_source']],height=45)
    if not c.get('events'):
        sheet.text('Подтверждённые наблюдения этого периода отсутствуют в доступных журналах; это не 0% отказов.')
    return sheet


def report_sheets(report, Sheet, include_details):
    sheets=[('Отчёт',main_sheet(report,Sheet))]
    if not include_details:
        return sheets
    names=case_names(report)
    if len(names)>500:
        raise ValueError('Более 500 катушек требуют отдельных карточек; сократите период. Неполный отчёт не выдан.')
    sheets.extend([('Динамика',daily_sheet(report,Sheet)),
                   ('Сверка',reconciliation_sheet(report,Sheet,names)),
                   ('Проверка катушек',review_sheet(report,Sheet,names)),
                   ('Распорядки 1С',request_sheet(report,Sheet)),
                   ('Связь RFID',communications_sheet(report,Sheet))])
    for code in CATEGORY_NAMES:
        if issue_counts(report)[code]:
            sheets.append((CATEGORY_NAMES[code],category_sheet(report,code,Sheet,names)))
    for index,row in enumerate((r for r in report['details'] if needs_review(r)),2):
        sheets.append((names[row['series']],case_sheet(report,row,Sheet,index)))
    sheets.append(('Методика',method_sheet(report,Sheet)))
    return sheets
