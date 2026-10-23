"""Read-only SQL loading and short HTTP requests for report snapshots.

Gateway has a 10-second upstream timeout. SQL/report work runs in one bounded
background worker; preview, download and the letter use the same snapshot.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
import os
import re
import secrets
import threading
import time

from flask import Response, jsonify, request

from common.efficiency_report import (build_efficiency_report, email_parts, methodology_text,
                                      preview_html, validate_period)
from common.efficiency_xlsx import generate_efficiency_xlsx


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
        fields = 'EventId,SourceTag,EPC,TID,FirstSeen,FinalDirection,RfidReadCount,SessionCloseReason,WarehouseId'
        condition = "ISNULL(RfidReadCount,0)>0 AND ISNULL(SessionCloseReason,'') NOT IN ('WAREHOUSE_ONLY','SUPERSEDED_BY_KPP')"
        events = read_rows(conn, f'''SELECT {fields} FROM dbo.KPP_ReelEvents
            WHERE FirstSeen>=? AND FirstSeen<? AND {condition} ORDER BY EventId''',
            (start-timedelta(hours=window), end+timedelta(hours=window)), 200000)
        # No cross-database Warehouse join: saved links may be older than the
        # fallback window, and the warehouse can live in a different database.
        ids = list(dict.fromkeys(int(row['Id']) for row in warehouse))
        by_id = {row['EventId']: row for row in events}
        for offset in range(0, len(ids), 500):
            batch = ids[offset:offset+500]
            linked = read_rows(conn, f'''SELECT {fields} FROM dbo.KPP_ReelEvents
                WHERE WarehouseId IN ({','.join('?' for _ in batch)}) AND {condition}''', batch, 200000)
            by_id.update((row['EventId'], row) for row in linked)
            if len(by_id) > 200000:
                raise ValueError('Слишком много событий; сократите период. Неполный отчёт не выдаётся.')
    return build_efficiency_report(warehouse, registry, by_id.values(), date_from, date_to, window_hours=window)


class ReportJobs:
    def __init__(self, loader, *, ttl=900, max_jobs=8):
        self.loader, self.ttl, self.max_jobs = loader, ttl, max_jobs
        self.lock = threading.Lock()
        self.slot = threading.BoundedSemaphore(1)
        self.jobs = {}

    def start(self, date_from, date_to, details=False):
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
            except ValueError as exc:
                result = dict(status='error', error=str(exc))
            except Exception:
                # ODBC errors can include connection details. Never expose them
                # in the browser, a letter, a workbook or public logs.
                result = dict(status='error', error='Не удалось прочитать все источники БД. Повторите формирование или обратитесь к администратору; неполный отчёт не выдан.')
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
            details = payload.get('include_details', False)
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
