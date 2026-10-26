"""Read-only SQL loading and short HTTP requests for report snapshots.

Gateway has a 10-second upstream timeout. SQL/report work runs in one bounded
background worker; preview, download and the letter use the same snapshot.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
import os
import logging
import re
import secrets
import threading
import time

from flask import Response, jsonify, request

from common.efficiency_report import (add_raw_evidence, build_efficiency_report, email_parts, local_time, methodology_text,
                                      preview_html, validate_period)
from common.efficiency_xlsx import generate_efficiency_xlsx
from common.efficiency_diagnostics import reconcile

log = logging.getLogger('perimeter.efficiency-report')


def sql_table(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*', value):
        raise ValueError('Некорректное имя таблицы отчёта')
    return value


def read_rows(connection, query, params, limit):
    cursor = connection.cursor()
    try:
        cursor.execute(query, *params)
        columns = [column[0] for column in cursor.description]
        rows = []
        while True:
            batch = cursor.fetchmany(1000)
            if not batch:
                return rows
            rows.extend(dict(zip(columns, row)) for row in batch)
            if len(rows) > limit:
                raise ValueError('Объём данных превышает лимит отчёта; сократите период. Неполный отчёт не выдаётся.')
    finally:
        cursor.close()


def fetch_efficiency_report(base, date_from, date_to):
    """Read complete sources, fail closed on SQL errors/truncation.

    Separate task DB follows the existing aggregator's KPP_TASK_CONN_STR.
    Do not use base.db_connect: its operational UI wrapper sets dirty reads.
    Reporting connections use READ COMMITTED with bounded lock waits.
    """
    validate_period(date_from, date_to)
    start = datetime.combine(date_from, datetime.min.time())
    end = datetime.combine(date_to + timedelta(days=1), datetime.min.time())
    window = 24
    warehouse_table = sql_table(os.getenv('KPP_WAREHOUSE_TABLE', 'dbo.Warehouse'))
    event_table = sql_table(os.getenv('KPP_EVENT_TABLE', 'dbo.KPP_ReelEvents'))
    task_table = sql_table(base.Config.TASK_TABLE)
    task_destination = os.getenv('KPP_TASK_CONN_STR') or base.Config.DB_CONN_STR

    def connect(destination):
        if not destination:
            raise ValueError('Не настроено подключение к БД отчёта')
        conn = base.pyodbc.connect(destination, timeout=5, autocommit=True, readonly=True)
        try:
            conn.timeout = 30
            cur = conn.cursor()
            try:
                cur.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                cur.execute('SET LOCK_TIMEOUT 2000')
            finally:
                cur.close()
        except Exception:
            conn.close()
            raise
        return conn

    with closing(connect(task_destination)) as conn:
        warehouse = read_rows(conn, f'''SELECT Id,Dt,Tag,Ids,SeriesNumber FROM {warehouse_table}
            WHERE Dt>=? AND Dt<? ORDER BY Dt,Id''', (start, end), 50000)
        # Historical registrations are essential (e.g. a May tag on an October
        # receipt). Never apply the aggregator's recent task cache window here.
        registry = read_rows(conn, f'''SELECT Id,Dt,Tag,Ids,SeriesNumber FROM {task_table}
            WHERE Dt<? OR Dt IS NULL ORDER BY Id''', (end,), 500000)
    with closing(connect(base.Config.DB_CONN_STR)) as conn:
        fields = 'EventId,SourceTag,EPC,TID,FirstSeen,LastSeen,FinalDirection,RfidReadCount,SessionCloseReason,WarehouseId,IsReel'
        condition = "ISNULL(RfidReadCount,0)>0 AND ISNULL(SessionCloseReason,'') NOT IN ('WAREHOUSE_ONLY','SUPERSEDED_BY_KPP')"
        events = read_rows(conn, f'''SELECT {fields} FROM {event_table}
            WHERE FirstSeen>=? AND FirstSeen<? AND {condition} ORDER BY EventId''',
            (start-timedelta(hours=window), end+timedelta(hours=window)), 200000)
        # No cross-database Warehouse join: saved links may be older than the
        # fallback window, and the warehouse can live in a different database.
        ids = list(dict.fromkeys(int(row['Id']) for row in warehouse))
        by_id = {row['EventId']: row for row in events}
        # An older session may still overlap the selected period. The ordinary
        # +/-24h receipt fallback is not a recognition-time limit.
        spanning = read_rows(conn, f'''SELECT {fields} FROM {event_table}
            WHERE FirstSeen<? AND LastSeen>=? AND {condition}''',
            (start-timedelta(hours=window), start), 200000)
        by_id.update((row['EventId'], row) for row in spanning)
        if len(by_id) > 200000:
            raise ValueError('Слишком много событий; сократите период. Неполный отчёт не выдаётся.')
        for offset in range(0, len(ids), 500):
            batch = ids[offset:offset+500]
            linked = read_rows(conn, f'''SELECT {fields} FROM {event_table}
                WHERE WarehouseId IN ({','.join('?' for _ in batch)}) AND {condition}''', batch, 200000)
            by_id.update((row['EventId'], row) for row in linked)
            if len(by_id) > 200000:
                raise ValueError('Слишком много событий; сократите период. Неполный отчёт не выдаётся.')
        report = build_efficiency_report(warehouse, registry, by_id.values(), date_from, date_to, window_hours=window)
        report['events_checked'] = True
        try:
            processing = fetch_processing_summary(conn, start, end)
        except Exception as exc:
            processing = dict(checked=False)
            log.warning('processing_log_check_failed error_class=%s', type(exc).__name__)
    # The antenna writer can use a different DB from the Web/Aggregator.
    raw_destination = os.getenv('RFID_DB_CONNECTION') or base.Config.DB_CONN_STR
    raw_table = sql_table(os.getenv('KPP_RFID_TABLE', 'dbo.RFID_Tags'))
    with closing(connect(raw_destination)) as conn:
        report['raw_source'] = fetch_raw_summary(conn, report, raw_table)
        raw_rows = fetch_raw_evidence(conn, report, raw_table)
        report = add_raw_evidence(report, raw_rows)
        if processing.get('cursor') is not None:
            try:
                processing.update(fetch_raw_progress(conn, report, raw_table, processing['cursor']))
            except Exception as exc:
                processing['pipeline_checked'] = False
                log.warning('raw_progress_check_failed error_class=%s', type(exc).__name__)
        try:
            history = fetch_raw_history(conn, report, raw_table)
        except Exception as exc:
            history = None
            log.warning('raw_history_check_failed error_class=%s', type(exc).__name__)
        try:
            report['delivery'] = fetch_delivery_summary(conn, report, raw_table)
        except Exception as exc:
            report['delivery'] = dict(checked=False)
            log.warning('delivery_check_failed error_class=%s', type(exc).__name__)
    return reconcile(report, history, processing=processing)


def fetch_raw_evidence(connection, report, raw_table):
    """Read actual Reader rows, never Warehouse.Tag or registry existence.

    The whole selected reporting period is evaluated in SQL and rechecked in the pure
    projection. Aggregate there, so millions of inventory reads are not sent to
    the Web process. A failed raw query aborts the report instead of counting 0.
    """
    candidates = set()
    start, end = raw_period(report)
    for row in report['details']:
        tags = set(row['registered_tags'])-set(row['ambiguous_tags'])
        for tag in tags:
            if len(tag) > 512:
                raise ValueError('Некорректная длина полной RFID-метки в реестре')
            candidates.add(tag)
    if len(candidates) > 100000:
        raise ValueError('Слишком много RFID-меток; сократите период. Неполный отчёт не выдаётся.')
    candidates = sorted(candidates)
    rows = []
    for offset in range(0, len(candidates), 200):
        batch = candidates[offset:offset+200]
        values = ','.join('(CONVERT(nvarchar(512),?))' for _ in batch)
        # SQLWriter stores the captured source_dt in both RecordTime and
        # SourceReaderTime (also on delayed spool replay). RecordTime has the
        # existing period index; wrapping it in COALESCE forces history scans.
        at = 'r.RecordTime'
        sql = f'''SELECT v.Tag,COUNT_BIG(*) AS ReadCount,
            MIN({at}) AS FirstRead,MAX({at}) AS LastRead,
            MIN(r.Id) AS FirstRawId,MAX(r.Id) AS LastRawId
            FROM (VALUES {values}) AS v(Tag)
            INNER JOIN {raw_table} r
              ON UPPER(REPLACE(REPLACE(REPLACE(REPLACE(
                 CONVERT(nvarchar(512),ISNULL(r.EPC,''))+CONVERT(nvarchar(512),ISNULL(r.TID,'')),
                 N' ',N''),NCHAR(9),N''),NCHAR(10),N''),NCHAR(13),N''))=v.Tag
            WHERE {at}>=? AND {at}<?
            GROUP BY v.Tag'''
        rows.extend(read_rows(connection, sql, [*batch, start, end], 200))
    return rows


def raw_period(report):
    return (datetime.fromisoformat(report['date_from']),
            datetime.fromisoformat(report['date_to'])+timedelta(days=1))


def fetch_raw_summary(connection, report, raw_table):
    start, end = raw_period(report)
    at = 'r.RecordTime'
    rows = read_rows(connection, f'''SELECT COUNT_BIG(*) AS raw_reads,
        MIN({at}) AS first_read,MAX({at}) AS last_read
        FROM {raw_table} r WHERE {at}>=? AND {at}<?''', (start, end), 1)
    if len(rows) != 1:
        raise ValueError('Не получена сводка исходного журнала антенны; отчёт не сформирован.')
    row = rows[0]
    return dict(raw_reads=int(row['raw_reads']),
                first_read=local_time(row['first_read']).isoformat() if row['first_read'] is not None else None,
                last_read=local_time(row['last_read']).isoformat() if row['last_read'] is not None else None)


def fetch_raw_history(connection, report, raw_table):
    """Seek the existing EPC index for missing 24-byte EPC+TID identities.

    Other tag formats remain explicitly unchecked. No SQL functions wrap EPC,
    no journal-wide scan per tag, no prefix match counted as a physical read.
    """
    tags = sorted({tag for row in report['details'] if row['has_rfid'] and not row['detected']
                   for tag in set(row['registered_tags'])-set(row['ambiguous_tags'])
                   if re.fullmatch('[0-9A-F]{48}', tag)})
    if len(tags) > 10000:
        raise ValueError('Слишком много меток для проверки истории')
    start, end = raw_period(report)
    rows = []
    for offset in range(0, len(tags), 200):
        batch = tags[offset:offset+200]
        values = ','.join('(CONVERT(varchar(48),?),CONVERT(varchar(100),?),CONVERT(varchar(200),?))' for _ in batch)
        sql = f'''SELECT v.Tag,
            SUM(CONVERT(bigint,CASE WHEN r.TID=v.TID THEN 1 ELSE 0 END)) AS ReadCount,
            SUM(CONVERT(bigint,CASE WHEN r.TID=v.TID AND r.RecordTime>=p.DateFrom AND r.RecordTime<p.DateTo THEN 1 ELSE 0 END)) AS PeriodReads,
            MIN(CASE WHEN r.TID=v.TID THEN r.RecordTime END) AS FirstRead,
            MAX(CASE WHEN r.TID=v.TID THEN r.RecordTime END) AS LastRead,
            MIN(CASE WHEN r.TID=v.TID THEN r.Id END) AS FirstRawId,
            MAX(CASE WHEN r.TID=v.TID THEN r.Id END) AS LastRawId,
            SUM(CONVERT(bigint,CASE WHEN r.RecordTime>=p.DateFrom AND r.RecordTime<p.DateTo THEN 1 ELSE 0 END)) AS EpcPeriodReads
            FROM (VALUES {values}) AS v(Tag,EPC,TID)
            CROSS JOIN (SELECT CONVERT(datetime2,?) AS DateFrom,CONVERT(datetime2,?) AS DateTo) p
            LEFT JOIN {raw_table} r ON r.EPC=v.EPC
            GROUP BY v.Tag'''
        params = [part for tag in batch for part in (tag, tag[:24], tag[24:])]
        rows.extend(read_rows(connection, sql, [*params, start, end], 200))
    return rows


def fetch_processing_summary(connection, start, end):
    rows = read_rows(connection, '''SELECT COUNT_BIG(*) AS total,
        COALESCE(SUM(CONVERT(bigint,CASE WHEN ResolvedAt IS NULL THEN 1 ELSE 0 END)),0) AS unresolved
        FROM dbo.KPP_ProcessingErrors WHERE CreatedAt>=? AND CreatedAt<?''', (start, end), 1)
    if len(rows) != 1:
        raise ValueError('Не прочитан журнал ошибок обработки')
    result = dict(checked=True, total=int(rows[0]['total']), unresolved=int(rows[0]['unresolved']))
    try:
        state_table=sql_table(os.getenv('KPP_STATE_TABLE','dbo.KPP_RuntimeState'))
        state = read_rows(connection, f"SELECT StateValue FROM {state_table} WHERE StateKey='LAST_RFID_ID_V3'", (), 1)
    except Exception as exc:
        state = []
        log.warning('raw_cursor_check_failed error_class=%s', type(exc).__name__)
    if state and str(state[0].get('StateValue', '')).isdigit():
        result['cursor'] = int(state[0]['StateValue'])
    return result


def fetch_raw_progress(connection, report, raw_table, cursor):
    start, end = raw_period(report)
    rows = read_rows(connection, f'''SELECT COUNT_BIG(*) AS read_total,
        COALESCE(SUM(CONVERT(bigint,CASE WHEN Id<=? THEN 1 ELSE 0 END)),0) AS consumed
        FROM {raw_table} WHERE RecordTime>=? AND RecordTime<?''', (int(cursor), start, end), 1)
    if len(rows) != 1:
        raise ValueError('Не подтверждено прохождение потока')
    total, consumed = int(rows[0]['read_total']), int(rows[0]['consumed'])
    if not 0 <= consumed <= total:
        raise ValueError('Некорректный счётчик обработки')
    return dict(pipeline_checked=True, read_total=total, consumed=consumed)


def fetch_delivery_summary(connection, report, raw_table):
    start, end = raw_period(report)
    valid = "ReceivedAt IS NOT NULL AND SourceReaderTime IS NOT NULL AND ReceivedAt>=SourceReaderTime AND TimeQuality IN ('HOST_CAPTURE_TIME','APPROXIMATE_AFTER_RECONNECT')"
    rows = read_rows(connection, f'''SELECT COUNT_BIG(*) AS total,
        COALESCE(SUM(CONVERT(bigint,CASE WHEN {valid} THEN 1 ELSE 0 END)),0) AS timed_rows,
        COALESCE(SUM(CONVERT(bigint,CASE WHEN {valid} AND ReceivedAt>DATEADD(second,120,SourceReaderTime) THEN 1 ELSE 0 END)),0) AS late_rows,
        MAX(CASE WHEN {valid} THEN DATEDIFF(second,SourceReaderTime,ReceivedAt) END) AS max_delay_sec,
        COALESCE(SUM(CONVERT(bigint,CASE WHEN TimeQuality='APPROXIMATE_AFTER_RECONNECT' THEN 1 ELSE 0 END)),0) AS after_reconnect
        FROM {raw_table} WHERE RecordTime>=? AND RecordTime<?''',(start,end),1)
    if len(rows)!=1:
        raise ValueError('Не прочитаны сведения о доставке RFID')
    row=rows[0]
    result=dict(checked=True,threshold_sec=120,**{key:int(row[key]) for key in ('total','timed_rows','late_rows','after_reconnect')})
    if not 0<=result['late_rows']<=result['timed_rows']<=result['total']:
        raise ValueError('Некорректные счётчики доставки RFID')
    result['max_delay_sec']=int(row['max_delay_sec']) if row['max_delay_sec'] is not None else None
    return result


class ReportJobs:
    def __init__(self, loader, *, ttl=900, max_jobs=8):
        self.loader, self.ttl, self.max_jobs = loader, ttl, max_jobs
        self.lock = threading.Lock()
        self.slot = threading.BoundedSemaphore(1)
        self.jobs = {}

    def start(self, date_from, date_to, details=True):
        if not self.slot.acquire(blocking=False):
            return None
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self.lock:
            self.jobs = {key: value for key, value in self.jobs.items() if now-value['created'] < self.ttl}
            while len(self.jobs) >= self.max_jobs:
                del self.jobs[next(iter(self.jobs))]
            self.jobs[token] = dict(created=now, status='pending')

        def work():
            try:
                report = self.loader(date_from, date_to)
                email = email_parts(report)
                result = dict(status='ready', report=report, email=email,
                              xlsx=generate_efficiency_xlsx(report, details), html=preview_html(report))
                log.info('report_ready from=%s to=%s total=%s tagged=%s detected=%s history_checked=%s duration_sec=%.3f',
                         date_from, date_to, report['totals']['total'], report['totals']['tagged'],
                         report['totals']['detected'], report.get('reconciliation', {}).get('history_checked'), time.monotonic()-now)
            except ValueError as exc:
                result = dict(status='error', error=str(exc))
                log.warning('report_validation_failed from=%s to=%s error_class=%s', date_from, date_to, type(exc).__name__)
            except Exception as exc:
                # ODBC errors can include connection details. Never expose them
                # in the browser, a letter, a workbook or public logs.
                result = dict(status='error', error='Не удалось прочитать все источники БД. Повторите формирование или обратитесь к администратору; неполный отчёт не выдан.')
                log.warning('report_failed from=%s to=%s error_class=%s', date_from, date_to, type(exc).__name__)
            finally:
                with self.lock:
                    job = self.jobs.get(token)
                    if job is not None:
                        job.update(result)
                        job['created'] = time.monotonic()
                self.slot.release()
        try:
            threading.Thread(target=work, name='rfid-efficiency-report', daemon=True).start()
        except Exception:
            with self.lock:
                self.jobs.pop(token, None)
            self.slot.release()
            raise
        return token

    def get(self, token):
        with self.lock:
            job = self.jobs.get(token)
            if job is None or time.monotonic()-job['created'] >= self.ttl:
                self.jobs.pop(token, None)
                return None
            return dict(job)


def install(base):
    jobs = ReportJobs(lambda date_from, date_to: fetch_efficiency_report(base, date_from, date_to))

    def reply(data, status=200):
        response = jsonify(data)
        response.status_code = status
        response.headers['Cache-Control'] = 'no-store'
        return response

    @base.app.route('/api/efficiency-report', methods=['POST'])
    def efficiency_start():
        payload = request.get_json(silent=True)
        try:
            if not isinstance(payload, dict):
                raise ValueError('Укажите даты от и до')
            raw_from, raw_to = payload.get('date_from'), payload.get('date_to')
            if not all(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) for value in (raw_from, raw_to)):
                raise ValueError('Укажите даты в формате ГГГГ-ММ-ДД')
            date_from, date_to = date.fromisoformat(raw_from), date.fromisoformat(raw_to)
            validate_period(date_from, date_to)
            details = payload.get('include_details', True)
            if type(details) is not bool:
                raise ValueError('Некорректный параметр подробной сверки')
        except (ValueError, TypeError) as exc:
            return reply(dict(error=str(exc)), 400)
        token = jobs.start(date_from, date_to, details)
        if token is None:
            return reply(dict(error='Отчёт уже формируется. Дождитесь завершения и повторите.'), 429)
        return reply(dict(job_id=token, status='pending'), 202)

    @base.app.route('/api/efficiency-report/<token>')
    def efficiency_status(token):
        job = jobs.get(token)
        if job is None:
            return reply(dict(error='Отчёт истёк или сменился рабочий узел. Сформируйте его заново.'), 404)
        if job['status'] != 'ready':
            return reply({key: job[key] for key in ('status', 'error') if key in job})
        report = job['report']
        return reply(dict(status='ready', html=job['html'], email={key: job['email'][key] for key in ('subject', 'text', 'html')},
                          methodology=methodology_text(report), generated_at=report['generated_at'],
                          totals=report['totals'], days=report['days'],
                          source_checks=dict(reader=report['raw_checked']), diagnostics=report['diagnostics'],
                          raw_stats=report['raw_stats'], raw_source=report.get('raw_source'),
                          reconciliation=report.get('reconciliation'),
                          delivery=report.get('delivery'),
                          download_url=f'/api/efficiency-report/{token}/download'))

    @base.app.route('/api/efficiency-report/<token>/download')
    def efficiency_download(token):
        job = jobs.get(token)
        if job is None:
            return reply(dict(error='Отчёт истёк или сменился рабочий узел. Сформируйте его заново.'), 404)
        if job['status'] != 'ready':
            return reply(dict(error=job.get('error', 'Отчёт ещё формируется')), 409)
        report = job['report']
        name = f'Perimeter_KPI_{report["date_from"]}_{report["date_to"]}.xlsx'
        return Response(job['xlsx'], mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': f'attachment; filename="{name}"', 'Cache-Control': 'no-store'})

    return jobs
