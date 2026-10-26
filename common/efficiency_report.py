"""Versioned, deterministic warehouse-cohort RFID effectiveness report.

This is a reporting projection, never an aggregator identity resolver. Several
registered full tags on one series are expected; a tag shared by different
series is ambiguous and cannot prove detection.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from html import escape
import re

MOSCOW = timezone(timedelta(hours=3))
VERSION = "1.5"
HEADERS = ("Дата", "Учтено Периметром", "Входные данные КПП", "Распознано КПП", "RFID КПП KPI", "Без входного чтения (сверка)")


def series_key(value):
    return str(value or "").strip().strip(",;. ").upper()


def tag_key(value):
    # Full EPC+TID, never a prefix match or a guessed replacement character.
    return re.sub(r"\s+", "", str(value or "")).upper()


def local_time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("В источнике отсутствует корректная дата")
    if value.tzinfo is not None:
        value = value.astimezone(MOSCOW).replace(tzinfo=None)
    return value


def validate_period(date_from, date_to):
    if not isinstance(date_from, date) or not isinstance(date_to, date):
        raise ValueError("Укажите даты от и до")
    if date_from > date_to:
        raise ValueError("Дата от должна быть не позже даты до")
    if (date_to - date_from).days >= 366:
        raise ValueError("Выберите период не более 366 дней")
    # datetime arithmetic and the exclusive end must remain representable.
    if date_from < date(1900, 1, 2) or date_to > date(9999, 12, 29):
        raise ValueError("Дата вне поддерживаемого диапазона")


def percent(numerator, denominator):
    if not denominator:
        return "—"
    return (f"{100 * numerator / denominator:.2f}".rstrip("0").rstrip(".").replace(".", ",") + "%")


def build_efficiency_report(warehouse_rows, registry_rows, event_rows, date_from, date_to,
                            *, window_hours=24, generated_at=None, raw_rows=None):
    validate_period(date_from, date_to)
    start = datetime.combine(date_from, datetime.min.time())
    end = datetime.combine(date_to + timedelta(days=1), datetime.min.time())
    warehouse = defaultdict(list)
    excluded_mk = 0
    for source in warehouse_rows:
        row = dict(source)
        dt = local_time(row.get("Dt"))
        if not start <= dt < end:
            continue
        series = series_key(row.get("SeriesNumber"))
        if not series:
            raise ValueError("В адресном складе есть запись без серии; отчёт не сформирован")
        if series.endswith(("МК", "MK")):
            excluded_mk += 1
            continue
        row["Dt"] = dt
        warehouse[series].append(row)

    registry = defaultdict(list)
    registrations = defaultdict(list)
    owners = defaultdict(set)
    all_owners = defaultdict(set)
    for source in registry_rows:
        series, tag = series_key(source.get("SeriesNumber")), tag_key(source.get("Tag"))
        if not tag or not series:
            continue
        dt = local_time(source.get("Dt"))
        registrations[series].append(dict(id=str(source.get('Id') or ''), dt=dt.isoformat(),
                                         tag=tag, document=str(source.get('Ids') or '')))
        all_owners[tag].add(series)
        if dt < end:
            registry[series].append((dt, tag))
            owners[tag].add(series)

    by_warehouse, by_tag, processed_by_tag, observed_by_tag = (defaultdict(list) for _ in range(4))
    processed_evidence=defaultdict(list)
    for source in event_rows:
        event = dict(source)
        if int(event.get("RfidReadCount") or 0) <= 0:
            continue
        if str(event.get("SessionCloseReason") or "").upper() in {"WAREHOUSE_ONLY", "SUPERSEDED_BY_KPP"}:
            continue
        tag = tag_key(event.get("SourceTag"))
        if not tag:
            tag = tag_key(event.get("EPC")) + tag_key(event.get("TID"))
        if not tag:
            continue
        event["tag"] = tag
        event["FirstSeen"] = local_time(event.get("FirstSeen"))
        event['LastSeen'] = local_time(event.get('LastSeen') or event['FirstSeen'])
        if event['LastSeen'] < event['FirstSeen']:
            raise ValueError('Некорректный временной интервал события RFID')
        if event['FirstSeen'] < end and event['LastSeen'] >= start:
            observed_by_tag[tag].append(str(event['EventId']))
            if str(event.get('IsReel', 1)).lower() in {'1', 'true'}:
                processed_by_tag[tag].append(str(event['EventId']))
                processed_evidence[tag].append(dict(id=str(event['EventId']),first=event['FirstSeen'].isoformat(),
                                                    last=event['LastSeen'].isoformat()))
        if event.get("WarehouseId") is not None:
            by_warehouse[str(event["WarehouseId"])].append(event)
        else:
            by_tag[tag].append(event)

    details = []
    delta = timedelta(hours=window_hours)
    for series, rows in warehouse.items():
        rows.sort(key=lambda row: (row["Dt"], str(row["Id"])))
        first = rows[0]
        registered = {tag for dt, tag in registry.get(series, ()) if dt <= first["Dt"]}
        ambiguous = {tag for tag in registered if len(owners[tag]) > 1}
        eligible = registered - ambiguous
        known_tags = {r['tag'] for r in registrations.get(series, ())}
        matched, event_ids = set(), set()
        for row in rows:
            # A saved WarehouseId wins, including accepted adaptive delays >24h.
            candidates = list(by_warehouse.get(str(row["Id"]), ()))
            for tag in eligible:
                candidates.extend(event for event in by_tag.get(tag, ())
                                  if abs(event["FirstSeen"] - row["Dt"]) <= delta
                                  and str(event.get("FinalDirection") or "").upper() != "IN")
            for event in candidates:
                if event["tag"] in eligible:
                    matched.add(event["tag"])
                    event_ids.add(str(event["EventId"]))
        details.append(dict(series=series, warehouse_dt=first["Dt"].isoformat(),
                            warehouse_ids=[str(row["Id"]) for row in rows],
                            warehouse_receipts=[dict(id=str(row['Id']), dt=row['Dt'].isoformat(),document=str(row.get('Ids') or '')) for row in rows],
                            registered_tags=sorted(registered), matched_tags=sorted(matched),
                            registrations=sorted(registrations.get(series, ()), key=lambda r: (r['dt'], r['id'])),
                            known_ambiguous_tags=sorted(tag for tag in known_tags
                                if len(owners.get(tag,all_owners[tag]))>1 or (tag in owners and series not in owners[tag])),
                            known_aggregator_tags=sorted(known_tags & processed_by_tag.keys()),
                            known_aggregator_evidence={tag:processed_evidence[tag] for tag in known_tags if tag in processed_evidence},
                            known_observed_event_ids=sorted({identity for tag in known_tags for identity in observed_by_tag.get(tag, ())}),
                            registration_dates=sorted({dt.isoformat() for dt, tag in registry.get(series, ()) if tag in registered and dt <= first['Dt']}),
                            event_ids=sorted(event_ids), ambiguous_tags=sorted(ambiguous),
                            aggregator_tags=sorted(eligible & processed_by_tag.keys()),
                            aggregator_event_ids=sorted({identity for tag in eligible for identity in processed_by_tag.get(tag, ())}),
                            observed_event_ids=sorted({identity for tag in eligible for identity in observed_by_tag.get(tag, ())}),
                            has_rfid=bool(registered), detected=bool(matched), in_perimeter=True))
    details.sort(key=lambda row: (row["warehouse_dt"], row["series"]))
    days = []
    current = date_from
    by_day = defaultdict(list)
    for row in details:
        by_day[row["warehouse_dt"][:10]].append(row)
    while current <= date_to:
        rows = by_day[current.isoformat()]
        days.append(dict(date=current.isoformat(), total=len(rows),
                         tagged=sum(row["has_rfid"] for row in rows),
                         detected=sum(row["detected"] for row in rows), perimeter=len(rows)))
        current += timedelta(days=1)
    totals = {key: sum(day[key] for day in days) for key in ("total", "tagged", "detected", "perimeter")}
    totals["missed"] = totals["tagged"] - totals["detected"]
    totals["without_rfid"] = totals["total"] - totals["tagged"]
    report = dict(version=VERSION, date_from=date_from.isoformat(), date_to=date_to.isoformat(),
                generated_at=(generated_at or datetime.now(MOSCOW)).isoformat(), days=days,
                totals=totals, details=details, excluded_mk=excluded_mk,
                duplicate_warehouse_rows=sum(len(rows)-1 for rows in warehouse.values()),
                window_hours=window_hours)
    return add_raw_evidence(report, raw_rows)


def add_raw_evidence(report, raw_rows):
    """Compare the 1C cohort with raw antenna reads in the selected period.

    Receipt timing, session direction and Aggregator linkage are diagnostics,
    not conditions for antenna detection. Each full tag and drum counts once.
    """
    report = dict(report)
    report['raw_checked'] = raw_rows is not None
    start = datetime.fromisoformat(report['date_from'])
    end = datetime.fromisoformat(report['date_to']) + timedelta(days=1)
    by_tag = defaultdict(list)
    seen = set()
    for raw in raw_rows or ():
        if int(raw.get('ReadCount') or 0) <= 0:
            continue
        tag = tag_key(raw.get('Tag'))
        first, last = local_time(raw['FirstRead']), local_time(raw['LastRead'])
        if not tag or not start <= first <= last < end:
            continue
        key = (tag, str(raw['FirstRawId']), str(raw['LastRawId']), first, last)
        if key in seen:
            continue
        seen.add(key)
        by_tag[tag].append(raw)
    details = []
    for original in report['details']:
        row = dict(original)
        kpp_tags = set(row.get('kpp_matched_tags', row['matched_tags']))
        eligible = set(row['registered_tags']) - set(row['ambiguous_tags'])
        raw_tags, proofs = set(), []
        for tag in sorted(eligible):
            for raw in by_tag.get(tag, ()):
                first, last = local_time(raw['FirstRead']), local_time(raw['LastRead'])
                raw_tags.add(tag)
                proofs.append(dict(tag=tag,
                                   first_read=first.isoformat(), last_read=last.isoformat(),
                                   read_count=int(raw['ReadCount']),
                                   first_id=str(raw['FirstRawId']), last_id=str(raw['LastRawId'])))
        # Keep later 1C instructions for chronology and software eligibility.
        # Warehouse timing must not prevent proving a read/request sequence.
        all_tags = set(row['registered_tags']) | {r['tag'] for r in row.get('registrations', ())}
        all_proofs=[]
        for tag in sorted(all_tags-set(row.get('known_ambiguous_tags', ()))):
            earliest=min((local_time(r['dt']) for r in row.get('registrations',()) if r['tag']==tag),default=None)
            for raw in by_tag.get(tag,()):
                first,last=local_time(raw['FirstRead']),local_time(raw['LastRead'])
                valid=local_time(raw['FirstEligibleRead']) if raw.get('FirstEligibleRead') else None
                if valid is None and earliest is not None:
                    if earliest<=first:valid=first
                    elif earliest<=last and int(raw['ReadCount'])<=2:
                        # With only two real reads, the endpoints are the complete set.
                        valid=last
                if valid is not None and not (first<=valid<=last and earliest is not None and valid>=earliest):
                    raise ValueError('Некорректное время первого чтения после распорядки')
                if earliest is not None and earliest<=last and valid is None:
                    raise ValueError('Не получено точное первое чтение после распорядки; повторите формирование.')
                all_proofs.append(dict(tag=tag,first_read=first.isoformat(),last_read=last.isoformat(),
                                       first_eligible_read=valid.isoformat() if valid else None,
                                       read_count=int(raw['ReadCount']),first_id=str(raw['FirstRawId']),last_id=str(raw['LastRawId'])))
        row.update(kpp_matched_tags=sorted(kpp_tags), raw_matched_tags=sorted(raw_tags),
                   all_raw_evidence=all_proofs,
                   raw_evidence=proofs, kpp_detected=bool(kpp_tags), raw_detected=bool(raw_tags),
                   matched_tags=sorted(raw_tags), detected=bool(raw_tags),
                   unread_tags=sorted(set(row['registered_tags'])-raw_tags))
        details.append(row)
    report['details'] = details
    days = [dict(day) for day in report['days']]
    by_day = defaultdict(int)
    for row in details:
        by_day[row['warehouse_dt'][:10]] += int(row['detected'])
    for day in days:
        day['detected'] = by_day[day['date']]
    report['days'] = days
    report['totals'] = dict(report['totals'])
    report['totals']['detected'] = sum(day['detected'] for day in days)
    report['totals']['missed'] = report['totals']['tagged']-report['totals']['detected']
    report['diagnostics'] = dict(kpp_confirmed=sum(row['kpp_detected'] for row in details),
                               raw_confirmed=sum(row['raw_detected'] for row in details),
                               reader_only=sum(row['raw_detected'] and not row['kpp_detected'] for row in details))
    registered = {tag for row in details for tag in row['registered_tags']}
    matched = {tag for row in details for tag in row['raw_matched_tags']}
    report['raw_stats'] = dict(registered_tags=len(registered), read_tags=len(matched),
                              unread_tags=len(registered-matched),
                              matched_reads=sum(int(raw['ReadCount']) for tag in matched for raw in by_tag[tag]))
    report['processing_coverage'] = dict(received=sum(row['raw_detected'] for row in details),
        recognized=sum(bool(set(row['raw_matched_tags']) & set(row.get('aggregator_tags', ()))) for row in details))
    from common.efficiency_accountability import assess_accountability
    return assess_accountability(report)


def raw_summary_text(report):
    if not report.get('raw_checked'):
        return 'Исходный журнал антенны не проверен.'
    s = report['raw_stats']
    total = report.get('raw_source', {}).get('raw_reads')
    prefix = f'Сырых чтений антенны за период — {total}; ' if total is not None else ''
    return (prefix+f'чтений меток отчётных барабанов — {s["matched_reads"]}. '
            f'Уникальных меток 1С — {s["registered_tags"]}, найдено в сырых чтениях — {s["read_tags"]}, '
            f'не подтверждено — {s["unread_tags"]}. '
            'Это сверка источников по барабанам; она не является KPI программной обработки КПП.')


def rfid_kpi(report):
    p=report.get('processing_coverage',{})
    return percent(p.get('recognized',0),p.get('received',0)) if report.get('raw_checked') and report.get('events_checked',True) else 'Не проверен'


def executive_rows(report):
    t=report['totals'];p=report.get('processing_coverage',{})
    checked=report.get('raw_checked') and report.get('events_checked',True)
    return [['Периметр целиком',f'{t["perimeter"]}/{t["total"]}',percent(t['perimeter'],t['total'])],
            ['RFID КПП: обработка доступных данных',f'{p.get("recognized",0)}/{p.get("received",0)}' if checked else 'Не проверено',
             percent(p.get('recognized',0),p.get('received',0)) if checked else 'Не проверен']]


def executive_text(report):
    return '\n'.join(f'{name}: {ratio} — {rate}' for name, ratio, rate in executive_rows(report))


def period_text(report):
    return "–".join(date.fromisoformat(report[key]).strftime("%d.%m.%Y") for key in ("date_from", "date_to"))


def summary_rows(report):
    software={d['date']:d for d in report.get('software_days',())}
    checked=report.get('raw_checked') and report.get('events_checked',True)
    missing=defaultdict(int)
    for row in report['details']:
        missing[row['warehouse_dt'][:10]]+=int(row.get('control',{}).get('status')=='NO_INPUT')
    rows=[]
    for day in report['days']:
        s=software.get(day['date'],dict(received=0,recognized=0))
        rows.append([date.fromisoformat(day['date']).strftime('%d.%m'),day['perimeter'],s['received'],
                     s['recognized'] if checked else 'Не проверено',percent(s['recognized'],s['received']) if checked else 'Не проверено',
                     missing[day['date']]])
    t=report['totals'];p=report.get('processing_coverage',{})
    rows.append(['Итого',t['perimeter'],p.get('received',0),p.get('recognized',0) if checked else 'Не проверено',
                 percent(p.get('recognized',0),p.get('received',0)) if checked else 'Не проверено',sum(missing.values())])
    return rows


def effectiveness_rows(report):
    t = report["totals"]
    p=report.get('processing_coverage',{})
    return [["1. Периметр целиком", t["total"], f'Периметр знает о {t["perimeter"]}', percent(t["perimeter"], t["total"])],
            ["2. RFID КПП: обработка доступных данных",p.get('received',0),f'Распознано {p.get("recognized",0)}',rfid_kpi(report)]]


def explanatory_text(report):
    t = report["totals"]
    return ("Поскольку RFID КПП «Периметр» получает данные не только от RFID-считывателей, но и из адресного склада, "
            "в общий отчёт включены также барабаны без зарегистрированных RFID-меток.\n\n"
            f'Из {t["total"]} барабанов система «Периметр» учитывает {t["perimeter"]} '
            f'({percent(t["perimeter"], t["total"])}). Без зарегистрированных меток в 1С — {t["without_rfid"]}.')


def exceptions_text(report, *, include_series=True):
    missed = [row["series"] for row in report["details"] if row["has_rfid"] and not row["detected"]]
    if missed:
        if not report.get('raw_checked'):
            return 'Исходный журнал RFID Reader не проверен. Окончательный KPI RFID-блока не рассчитан.'
        return (f'Считывание RFID не подтверждено у {len(missed)} барабанов с зарегистрированными метками'
                + (': '+', '.join(missed) if include_series else '')
                + '. Требуется проверка фактического наличия меток и истории перемещения.')
    if report["totals"]["tagged"]:
        return "Пропусков среди барабанов с зарегистрированными RFID-метками не выявлено."
    return "В выбранном периоде нет барабанов с зарегистрированными RFID-метками; RFID KPI не рассчитывается."


def methodology_text(report):
    return ('Периметр целиком: учёт уникальных серий складской выборки через RFID КПП и адресный склад. '
            'RFID КПП KPI: распознанные катушки / катушки, для которых получено точное сырое чтение и к этому чтению '
            'передана распорядка 1С с тем же полным EPC+TID. Это оценка обработки доступных данных, '
            'не процент физических считываний всех складских катушек. '
            'RfidTags.Dt означает момент передачи распорядки Периметру согласно подтверждённому владельцем правилу обмена. '
            'Распорядка действует до исполнения без срока; возраст записи и дата Warehouse не прекращают контроль. '
            'Явного статуса закрытия в реестре нет: старое связанное событие не объявляется закрытием заявки. '
            'Warehouse — последующий этап и контекст последовательности, а не срок поступления распорядки. '
            'Заявка после чтения и заявка после складской записи показываются с ID и временами источников. '
            'Нет исходного чтения при наличии заявки — отдельный неоценённый случай, причина без доказательства не назначается. '
            'Распознавание подтверждается физическим событием IsReel=1 с полной меткой; Warehouse-only не является чтением. '
            'Серии учитываются один раз за период, МК/MK исключены, неоднозначные метки не доказывают обнаружение. '
            'Дневной RFID KPI привязан к первому доступному чтению; общий учёт и позиции без чтения — к складской выборке. '
            'RecordTime — время захвата записи Reader, не ReceivedAt; чтения после переподключения могут иметь приблизительное время. '
            'Локальные ротации журнала Reader проверяются автоматически при доступности, но не заменяют полную историю HA. '
            'Доставка Reader → SQL измеряет задержку цепочки, не причину сетевого отказа. Время — МСК; источники читаются последовательно.')


def table_html(headers, rows):
    return ('<table class="eff-table"><thead><tr>' + ''.join('<th>'+escape(str(x))+'</th>' for x in headers)
            + '</tr></thead><tbody>' + ''.join('<tr>'+''.join('<td>'+escape(str(x))+'</td>' for x in row)+'</tr>' for row in rows)
            + '</tbody></table>')


def email_parts(report):
    from common.efficiency_accountability import assess_accountability
    if 'accountability' not in report:
        report=assess_accountability(report)
    a=report['accountability']
    subject=f'Отчёт по работе КПП «Периметр» за {period_text(report)}'
    blocks=['Добрый день, Павел Валерьевич!',f'Итоги за {period_text(report)}.',executive_text(report),
            'RFID KPI оценивает обработку полученных чтений при наличии распорядки 1С.',
            f'Вне этой оценки: нет распорядки 1С — {a["no_instruction"]}; '
            f'исходное чтение при наличии заявки не поступило — {a["unresolved_no_input"]}. '
            f'Распорядки постфактум — {a["late_transfers"]}.',
            (f'Распознавание при доступных входных данных требует проверки у {a["processing_review"]} катушек.'
             if report.get('events_checked',True) else 'События обработки не проверены.'),
            'Причины отсутствия чтений распределяются по подтверждённым данным; неустановленные причины выделены отдельно.',
            'Хронология заявок 1С, чтений, складских записей и журналов связи — в Excel.']
    if not report['totals']['tagged'] and not report.get('processing_coverage',{}).get('received'):
        blocks.insert(3,'RFID KPI не рассчитывается: доступных чтений с распорядкой нет.')
    html=''.join(table_html(['Показатель','Результат / база','Эффективность'],executive_rows(report)) if i==2
                 else '<p>'+escape(text).replace('\n','<br>')+'</p>' for i,text in enumerate(blocks))
    return dict(subject=subject,text='\n\n'.join(blocks),html=html,blocks=blocks)


def preview_html(report):
    from common.efficiency_diagnostics import reconcile
    from common.efficiency_cases import short_scorecard, issue_counts, CATEGORY_NAMES
    if 'reconciliation' not in report:
        report = reconcile(report)
    return (f'<h3>Работа КПП «Периметр»</h3><p>Период: {period_text(report)}</p>'
            + table_html(['Показатель', 'Учтено', 'База', 'Результат', 'Что измерено'], short_scorecard(report))
            + table_html(['Расхождение', 'Катушек'],
                         [[CATEGORY_NAMES[code], count] for code, count in issue_counts(report).items()])
            + '<p>В Excel: сверка всех катушек, графики и индивидуальные карточки расхождений. Письмо доступно отдельно.</p>')
