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
VERSION = "1.0"
HEADERS = ("Дата", "Всего барабанов", "С RFID", "RFID считано", "RFID KPI", "Периметр целиком")


def series_key(value):
    return str(value or "").strip().strip(",; ").upper()


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
                            *, window_hours=24, generated_at=None):
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
    owners = defaultdict(set)
    for source in registry_rows:
        series, tag = series_key(source.get("SeriesNumber")), tag_key(source.get("Tag"))
        if not tag or not series:
            continue
        dt = local_time(source.get("Dt"))
        if dt < end:
            registry[series].append((dt, tag))
            owners[tag].add(series)

    by_warehouse, by_tag = defaultdict(list), defaultdict(list)
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
                            registered_tags=sorted(registered), matched_tags=sorted(matched),
                            event_ids=sorted(event_ids), ambiguous_tags=sorted(ambiguous),
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
    return dict(version=VERSION, date_from=date_from.isoformat(), date_to=date_to.isoformat(),
                generated_at=(generated_at or datetime.now(MOSCOW)).isoformat(), days=days,
                totals=totals, details=details, excluded_mk=excluded_mk,
                duplicate_warehouse_rows=sum(len(rows)-1 for rows in warehouse.values()),
                window_hours=window_hours)


def period_text(report):
    return "–".join(date.fromisoformat(report[key]).strftime("%d.%m.%Y") for key in ("date_from", "date_to"))


def summary_rows(report):
    rows = [[date.fromisoformat(day["date"]).strftime("%d.%m"), day["total"], day["tagged"], day["detected"],
             percent(day["detected"], day["tagged"]), f'{day["perimeter"]}/{day["total"]}'] for day in report["days"]]
    t = report["totals"]
    rows.append(["Итого", t["total"], t["tagged"], t["detected"], percent(t["detected"], t["tagged"]),
                 f'{t["perimeter"]}/{t["total"]} = {percent(t["perimeter"], t["total"])}'])
    return rows


def effectiveness_rows(report):
    t = report["totals"]
    return [["1. Только барабаны с RFID", t["tagged"], f'RFID считано у {t["detected"]}', percent(t["detected"], t["tagged"])],
            ["2. Все барабаны, Периметр целиком", t["total"], f'Периметр знает о {t["perimeter"]}', percent(t["perimeter"], t["total"])]]


def explanatory_text(report):
    t = report["totals"]
    return ("Поскольку RFID КПП «Периметр» получает данные не только от RFID-считывателей, но и из адресного склада, "
            "в общий отчёт включены также барабаны без зарегистрированных RFID-меток.\n\n"
            f'Из {t["total"]} барабанов система «Периметр» учитывает {t["perimeter"]} '
            f'({percent(t["perimeter"], t["total"])}). Без зарегистрированных меток в 1С — {t["without_rfid"]}.')


def exceptions_text(report):
    missed = [row["series"] for row in report["details"] if row["has_rfid"] and not row["detected"]]
    if missed:
        return (f'Считывание RFID не подтверждено у {len(missed)} барабанов с зарегистрированными метками: '
                + ", ".join(missed) + ". Требуется проверка журналов и фактического наличия меток.")
    if report["totals"]["tagged"]:
        return "Пропусков среди барабанов с зарегистрированными RFID-метками не выявлено."
    return "В выбранном периоде нет барабанов с зарегистрированными RFID-метками; RFID KPI не рассчитывается."


def methodology_text(report):
    return ("Состав: уникальные серии адресного склада; повторные записи одной серии за период учтены один раз, "
            "по дате первой приёмки. Серии с суффиксом МК/MK исключены. Источник Warehouse не содержит вида тары; "
            "прочие серии относятся к барабанам по принятой номенклатуре учёта. "
            "С RFID: полный Tag зарегистрирован в 1С не позже первой приёмки. RFID считано: хотя бы один такой Tag "
            "совпадает с физическим событием КПП (RfidReadCount > 0); складская запись сама по себе не является считыванием. "
            f'Связь: сохранённый WarehouseId, иначе полный Tag в пределах ±{report["window_hours"]} ч без возврата IN. '
            "Метка, связанная с несколькими сериями, не доказывает обнаружение. Регистрация в 1С не гарантирует сохранность "
            "физической метки. Периметр целиком включает прямые записи адресного склада. "
            "Время — МСК. Источники читаются последовательно, без общей транзакционной точки снимка.")


def table_html(headers, rows):
    return ('<table class="eff-table"><thead><tr>' + ''.join('<th>'+escape(str(x))+'</th>' for x in headers)
            + '</tr></thead><tbody>' + ''.join('<tr>'+''.join('<td>'+escape(str(x))+'</td>' for x in row)+'</tr>' for row in rows)
            + '</tbody></table>')


def email_parts(report):
    subject = f'Отчёт по эффективности RFID КПП «Периметр» за {period_text(report)}'
    intro = f'Ниже представлен отчёт по эффективности считывания RFID-меток системой КПП «Периметр» за период {period_text(report)}.'
    footer = (f'Эффективность обнаружения барабанов с зарегистрированными RFID-метками — '
              f'{percent(report["totals"]["detected"], report["totals"]["tagged"])}; '
              f'полнота общего учёта — {percent(report["totals"]["perimeter"], report["totals"]["total"])}.')
    blocks = ["Добрый день, Павел Валерьевич!", intro,
              '\n'.join('\t'.join(map(str, row)) for row in [HEADERS, *summary_rows(report)]),
              explanatory_text(report), exceptions_text(report), "Итоговая эффективность:",
              '\n'.join('\t'.join(map(str, row)) for row in [["Показатель", "Склад / 1С", "Периметр", "Эффективность"], *effectiveness_rows(report)]), footer]
    html = ''.join('<p>'+escape(text).replace('\n', '<br>')+'</p>' if i not in {2, 6} else
                   table_html(HEADERS, summary_rows(report)) if i == 2 else
                   table_html(["Показатель", "Склад / 1С", "Периметр", "Эффективность"], effectiveness_rows(report))
                   for i, text in enumerate(blocks))
    return dict(subject=subject, text='\n\n'.join(blocks), html=html, blocks=blocks)


def preview_html(report):
    warning = ""
    if any(row["ambiguous_tags"] for row in report["details"]):
        warning = '<p role="alert">Есть метки, зарегистрированные за несколькими сериями. Их считывание не засчитано.</p>'
    return (f'<h3>Эффективность RFID КПП «Периметр»</h3><p>Период: {period_text(report)}</p>'
            + table_html(HEADERS, summary_rows(report)) + '<p>'+escape(explanatory_text(report)).replace('\n', '<br>')+'</p>'
            + table_html(["Показатель", "Склад / 1С", "Периметр", "Эффективность"], effectiveness_rows(report))
            + '<p>'+escape(exceptions_text(report))+'</p>'+warning)
