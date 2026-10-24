"""Offline business, export and HTTP regressions; never contacts factory SQL."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import importlib
import io
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from common.efficiency_report import (add_raw_evidence, build_efficiency_report, email_parts, percent, preview_html,
                                      validate_period)
from common.efficiency_xlsx import generate_efficiency_xlsx


def reference_population():
    """Synthetic fixture with the accepted 19–22 Oct distribution, no real EPCs."""
    warehouse, registry, events = [], [], []
    for day, total, tagged, detected in [(19, 24, 1, 0), (20, 34, 24, 24), (21, 40, 24, 24), (22, 33, 32, 32)]:
        for number in range(total):
            identity = len(warehouse)+1
            series = '4265/26' if identity == 1 else f'{10000+identity}/26'
            dt = datetime(2026, 10, day, 14, number)
            warehouse.append(dict(Id=identity, Dt=dt, Tag=None, Ids=f'doc{identity}', SeriesNumber=series))
            if number < tagged:
                for tag_number in range(2 if detected else 8):
                    tag = f'{identity:024X}{tag_number:024X}'
                    registry.append(dict(Id=len(registry)+1, Dt=datetime(2026, 5, 23), Tag=tag, SeriesNumber=series))
                    if number < detected:
                        events.append(dict(EventId=len(events)+1, SourceTag=tag, FirstSeen=dt-timedelta(minutes=10),
                                           WarehouseId=identity, RfidReadCount=10, FinalDirection='OUT'))
    return warehouse, registry, events


def reference_raw_reads(warehouse, registry):
    """Independent synthetic antenna journal: no session/link fields required."""
    receipts = {row['SeriesNumber']: row for row in warehouse}
    return [dict(Tag=tag['Tag'], ReadCount=10, FirstRead=receipts[tag['SeriesNumber']]['Dt']-timedelta(minutes=10),
                 LastRead=receipts[tag['SeriesNumber']]['Dt'], FirstRawId=tag['Id']*10, LastRawId=tag['Id']*10+9)
            for tag in registry if tag['SeriesNumber'] in receipts
            and receipts[tag['SeriesNumber']]['Dt'].day in (20, 21, 22)
            and tag['Dt'] <= receipts[tag['SeriesNumber']]['Dt']]


class EfficiencyReportTests(unittest.TestCase):
    def setUp(self):
        self.w, self.r, self.e = reference_population()
        self.start, self.end = date(2026, 10, 19), date(2026, 10, 22)

    def report(self):
        return build_efficiency_report(self.w, self.r, self.e, self.start, self.end,
                                       raw_rows=reference_raw_reads(self.w, self.r))

    def test_reference_weighted_totals_and_old_registry(self):
        report = self.report()
        self.assertEqual(report['totals'], dict(total=131, tagged=81, detected=80, perimeter=131, missed=1, without_rfid=50))
        self.assertEqual([(r['total'], r['tagged'], r['detected']) for r in report['days']],
                         [(24, 1, 0), (34, 24, 24), (40, 24, 24), (33, 32, 32)])
        self.assertEqual(percent(80, 81), '98,77%')
        self.assertEqual([r['series'] for r in report['details'] if r['has_rfid'] and not r['detected']], ['4265/26'])

    def test_duplicate_warehouse_and_events_do_not_inflate_drums(self):
        self.w.extend([dict(self.w[0], Id=999, SeriesNumber=' 4265/26, '), dict(self.w[-1], Id=1000)])
        self.e.extend(self.e[:10])
        report = self.report()
        self.assertEqual(report['totals']['total'], 131)
        self.assertEqual(report['totals']['detected'], 80)
        self.assertEqual(report['duplicate_warehouse_rows'], 2)

    def test_registered_tag_copied_to_warehouse_only_is_not_a_read(self):
        self.e.append(dict(EventId=9999, SourceTag=self.r[0]['Tag'], FirstSeen=self.w[0]['Dt'],
                           RfidReadCount=0, WarehouseId=1, SessionCloseReason='WAREHOUSE_ONLY'))
        self.assertEqual(self.report()['totals']['detected'], 80)
        self.e[-1]['RfidReadCount'] = 99
        self.assertEqual(self.report()['totals']['detected'], 80)

    def test_late_registration_cannot_change_past_denominator(self):
        self.r.append(dict(Id=90000, Dt=datetime(2026, 10, 23), Tag='A'*48, SeriesNumber=self.w[1]['SeriesNumber']))
        self.assertEqual(self.report()['totals']['tagged'], 81)

    def test_tag_prefix_or_wrong_tid_does_not_prove_read(self):
        self.e.append(dict(EventId=9999, SourceTag=self.r[0]['Tag'][:24], FirstSeen=self.w[0]['Dt'],
                           RfidReadCount=1, WarehouseId=1))
        self.assertEqual(self.report()['totals']['detected'], 80)

    def test_shared_tag_is_ambiguous_even_with_saved_warehouse_link(self):
        tag = self.e[0]['SourceTag']
        self.r.append(dict(Id=90000, Dt=datetime(2026, 1, 1), Tag=tag, SeriesNumber='another/26'))
        # The same drum has another legitimate full tag, so detection remains.
        self.assertEqual(self.report()['totals']['detected'], 80)
        self.e = [r for r in self.e if r['WarehouseId'] != self.e[0]['WarehouseId'] or r['SourceTag'] == tag]
        self.assertEqual(self.report()['diagnostics']['kpp_confirmed'], 79)
        self.assertEqual(self.report()['totals']['detected'], 80)

    def test_persisted_link_survives_adaptive_delay(self):
        for event in self.e:
            event['FirstSeen'] -= timedelta(days=4)
        self.assertEqual(self.report()['totals']['detected'], 80)

    def test_full_tag_fallback_respects_window_and_inbound_direction(self):
        for event in self.e:
            event['WarehouseId'] = None
        self.assertEqual(self.report()['totals']['detected'], 80)
        for event in self.e:
            event['FinalDirection'] = 'IN'
        self.assertEqual(self.report()['diagnostics']['kpp_confirmed'], 0)
        self.assertEqual(self.report()['totals']['detected'], 80)
        for event in self.e:
            event['FinalDirection'] = 'OUT'
            event['FirstSeen'] -= timedelta(days=2)
        self.assertEqual(self.report()['diagnostics']['kpp_confirmed'], 0)
        self.assertEqual(self.report()['totals']['detected'], 80)

    def test_event_linked_elsewhere_is_not_reused(self):
        for event in self.e:
            event['WarehouseId'] = 999999
        self.assertEqual(self.report()['diagnostics']['kpp_confirmed'], 0)
        self.assertEqual(self.report()['totals']['detected'], 80)

    def test_mk_exclusion_dates_and_timezone(self):
        self.w.append(dict(Id=999, Dt=datetime(2026, 10, 21), SeriesNumber='123/26МК'))
        self.w.append(dict(Id=1000, Dt='2026-10-18T22:00:00+00:00', SeriesNumber='utc/26'))
        self.w.append(dict(Id=1001, Dt=datetime(2026, 10, 23), SeriesNumber='outside/26'))
        report = self.report()
        self.assertEqual(report['totals']['total'], 132)
        self.assertEqual(report['days'][0]['total'], 25)
        self.assertEqual(report['excluded_mk'], 1)

    def test_empty_report_is_not_one_hundred_percent(self):
        report = build_efficiency_report([], [], [], self.start, self.end, raw_rows=[])
        self.assertEqual(len(report['days']), 4)
        self.assertEqual(percent(0, 0), '—')
        self.assertNotIn('100%', email_parts(report)['text'])
        self.assertIn('не рассчитывается', email_parts(report)['text'])

    def test_invalid_period_and_invalid_warehouse_fail(self):
        for start, end in [(self.end, self.start), (date(2025, 1, 1), self.end)]:
            with self.assertRaises(ValueError): validate_period(start, end)
        self.w[0]['SeriesNumber'] = ''
        with self.assertRaisesRegex(ValueError, 'без серии'): self.report()

    def test_letter_matches_summary_and_html_is_escaped(self):
        report = self.report()
        letter = email_parts(report)
        for value in ['98,77%', '131/131 = 100%', 'RFID-блок', 'Периметр целиком', 'Павел Валерьевич']:
            self.assertIn(value, letter['text'])
        self.assertNotIn('будет проверена', letter['text'])
        report['details'][0]['series'] = '<img src=x onerror=alert(1)>'
        self.assertNotIn('<img', preview_html(report))
        self.assertNotIn('<img', email_parts(report)['html'])

    def test_xlsx_one_sheet_default_formulas_cached_values_and_optional_details(self):
        for details in (False, True):
            with zipfile.ZipFile(io.BytesIO(generate_efficiency_xlsx(self.report(), details))) as archive:
                for name in archive.namelist():
                    ET.fromstring(archive.read(name))
                ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                workbook = ET.fromstring(archive.read('xl/workbook.xml'))
                self.assertEqual(len(workbook.findall('m:sheets/m:sheet', ns)), 2 if details else 1)
                sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
                cells = {c.attrib['r']: c for c in sheet.findall('.//m:c', ns)}
                self.assertEqual(cells['B11'].find('m:v', ns).text, '131')
                self.assertEqual(cells['C11'].find('m:v', ns).text, '81')
                self.assertEqual(cells['D11'].find('m:v', ns).text, '80')
                self.assertEqual(cells['E11'].find('m:f', ns).text, 'IF(C11=0,"",D11/C11)')
                self.assertAlmostEqual(float(cells['E11'].find('m:v', ns).text), 80/81)
                self.assertIn('ТЕКСТ ПИСЬМА', archive.read('xl/worksheets/sheet1.xml').decode())

    def raw(self, identity=1, **changes):
        warehouse = next(row for row in self.w if row['Id'] == identity)
        registry = next(row for row in self.r if row['SeriesNumber'] == warehouse['SeriesNumber'])
        row = dict(Tag=registry['Tag'], ReadCount=20,
                   FirstRead=warehouse['Dt']-timedelta(minutes=20), LastRead=warehouse['Dt']-timedelta(minutes=19),
                   FirstRawId=100001, LastRawId=100020)
        row.update(changes)
        return row

    def test_reader_read_without_aggregator_event_counts_as_detected(self):
        report = add_raw_evidence(self.report(), [*reference_raw_reads(self.w, self.r), self.raw()])
        self.assertEqual(report['totals']['detected'], 81)
        self.assertEqual(report['totals']['missed'], 0)
        self.assertEqual(report['diagnostics']['reader_only'], 1)
        self.assertIn('RFID-блок: 81/81 — 100%', email_parts(report)['text'])

    def test_low_aggregator_coverage_is_not_substituted_for_reader_kpi(self):
        # Only 36 series have Aggregator events; raw Reader proves 80.
        detected_ids = sorted({row['WarehouseId'] for row in self.e})
        keep = set(detected_ids[:36])
        self.e = [row for row in self.e if row['WarehouseId'] in keep]
        report = self.report()
        self.assertEqual(report['diagnostics']['kpp_confirmed'], 36)
        self.assertEqual(report['totals']['detected'], 80)
        self.assertIn('RFID-блок: 80/81 — 98,77%', email_parts(report)['text'])

    def test_genuinely_low_reader_and_aggregator_result_is_not_inflated(self):
        keep = set(sorted({row['WarehouseId'] for row in self.e})[:36])
        report = add_raw_evidence(self.report(), [self.raw(identity) for identity in keep])
        self.assertEqual(report['diagnostics']['kpp_confirmed'], 80)
        self.assertEqual(report['totals']['detected'], 36)
        self.assertIn('44,44%', email_parts(report)['text'])
        self.assertNotIn('98,77%', email_parts(report)['text'])

    def test_aggregator_does_not_replace_empty_raw_journal(self):
        report = add_raw_evidence(self.report(), [])
        self.assertEqual(report['diagnostics']['kpp_confirmed'], 80)
        self.assertEqual(report['totals']['detected'], 0)
        self.assertEqual(report['raw_stats']['matched_reads'], 0)

    def test_unchecked_reader_cannot_be_exported_as_final_kpi(self):
        report = build_efficiency_report(self.w, self.r, self.e, self.start, self.end)
        self.assertIn('Не проверен', email_parts(report)['text'])
        self.assertNotIn('98,77%', email_parts(report)['text'])
        with self.assertRaisesRegex(ValueError, 'Reader'):
            generate_efficiency_xlsx(report)

    def test_raw_evidence_requires_exact_tag_and_selected_period(self):
        for changes in [dict(Tag='F'*48), dict(ReadCount=0),
                        dict(FirstRead=self.w[0]['Dt']-timedelta(days=10)),
                        dict(LastRead=self.w[0]['Dt']+timedelta(days=10))]:
            with self.subTest(changes=changes):
                report = add_raw_evidence(self.report(), [*reference_raw_reads(self.w, self.r), self.raw(**changes)])
                self.assertEqual(report['totals']['detected'], 80)

    def test_duplicate_raw_proofs_and_multiple_tags_do_not_inflate_drums(self):
        proof = self.raw()
        other = self.raw(Tag=self.r[1]['Tag'])
        report = add_raw_evidence(self.report(), [*reference_raw_reads(self.w, self.r), proof, proof, other])
        self.assertEqual(report['totals']['detected'], 81)
        self.assertEqual(report['totals']['tagged'], 81)
        self.assertEqual(report['raw_stats']['matched_reads'], 1640)

    def test_raw_read_two_days_from_receipt_counts_within_selected_period(self):
        self.e = []
        report = add_raw_evidence(self.report(), [self.raw(FirstRead=datetime(2026, 10, 21, 8),
                                                         LastRead=datetime(2026, 10, 21, 9))])
        self.assertEqual(report['totals']['detected'], 1)
        self.assertEqual(report['diagnostics']['kpp_confirmed'], 0)
        self.assertEqual(report['days'][0]['detected'], 1)

    def test_selected_period_boundaries_are_half_open(self):
        for at, expected in [(datetime(2026, 10, 19), 1), (datetime(2026, 10, 23), 0),
                             (datetime(2026, 10, 18, 23, 59, 59), 0)]:
            with self.subTest(at=at):
                report = add_raw_evidence(self.report(), [self.raw(FirstRead=at, LastRead=at)])
                self.assertEqual(report['totals']['detected'], expected)

    def test_many_repeated_reads_never_replace_missing_drums_or_tags(self):
        report = add_raw_evidence(self.report(), [self.raw(ReadCount=1000000)])
        self.assertEqual(report['totals']['detected'], 1)
        self.assertEqual(report['raw_stats']['matched_reads'], 1000000)
        self.assertEqual(report['raw_stats']['read_tags'], 1)
        self.assertEqual(report['raw_stats']['unread_tags'], 167)

    def test_series_terminal_period_is_normalized_without_blacklisting(self):
        self.w.append(dict(self.w[0], Id=999, SeriesNumber='4265/26.'))
        report = self.report()
        self.assertEqual(report['totals']['total'], 131)
        self.assertEqual(report['duplicate_warehouse_rows'], 1)

    def test_long_series_text_export_terminates_and_preserves_characters(self):
        report = self.report()
        report['details'][0]['series'] = 'X'*4000
        # Series lists live in diagnostics; the management letter has a count.
        self.assertNotIn('X'*4000, email_parts(report)['text'])
        self.assertTrue(generate_efficiency_xlsx(report, True).startswith(b'PK'))


class EfficiencyHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'web'))
        try:
            cls.web = importlib.import_module('kpp_reel_dashboard_v3_fixed')
            cls.api = importlib.import_module('efficiency_api')
        finally:
            sys.path.pop(0)
        cls.base = cls.web.base

    def setUp(self):
        auth = patch.object(self.base.Config, 'AUTH_REQUIRED', False)
        auth.start(); self.addCleanup(auth.stop)
        self.client = self.base.app.test_client()
        self.web.efficiency_jobs.jobs.clear()

    def wait_job(self, token):
        for _ in range(200):
            response = self.client.get('/api/efficiency-report/'+token)
            if response.get_json().get('status') != 'pending': return response
            time.sleep(.01)
        self.fail('Report worker did not finish')

    def test_full_async_http_flow_uses_one_snapshot(self):
        w, r, e = reference_population()
        report = build_efficiency_report(w, r, e, date(2026, 10, 19), date(2026, 10, 22), raw_rows=reference_raw_reads(w, r))
        with patch.object(self.web.efficiency_jobs, 'loader', return_value=report) as loader:
            start = self.client.post('/api/efficiency-report', json=dict(date_from='2026-10-19', date_to='2026-10-22'))
            self.assertEqual(start.status_code, 202)
            token = start.get_json()['job_id']
            result = self.wait_job(token)
            self.assertEqual(result.get_json()['totals']['detected'], 80)
            self.assertIn('98,77%', result.get_json()['email']['text'])
            download = self.client.get(result.get_json()['download_url'])
            self.assertEqual(download.status_code, 200)
            self.assertTrue(download.data.startswith(b'PK'))
            self.assertEqual(download.headers['Cache-Control'], 'no-store')
            loader.assert_called_once()

    def test_auth_and_input_validation_precede_sql(self):
        with patch.object(self.web.efficiency_jobs, 'loader') as loader:
            for payload in [None, {}, [], dict(date_from='2026-10-23', date_to='2026-10-19'),
                            dict(date_from='2026-99-99', date_to='2026-10-19'),
                            dict(date_from='2026-10-19junk', date_to='2026-10-22'),
                            dict(date_from='2026-10-19', date_to='2026-10-22', include_details='yes')]:
                response = self.client.post('/api/efficiency-report', json=payload)
                self.assertEqual(response.status_code, 400, payload)
            with patch.object(self.base.Config, 'AUTH_REQUIRED', True):
                self.assertEqual(self.client.post('/api/efficiency-report', json={}).status_code, 401)
                self.assertEqual(self.client.get('/api/efficiency-report/token').status_code, 401)
                self.assertEqual(self.client.get('/api/efficiency-report/token/download').status_code, 401)
            loader.assert_not_called()

    def test_sql_failure_does_not_become_zero_or_leak_connection(self):
        with patch.object(self.web.efficiency_jobs, 'loader', side_effect=RuntimeError('PWD=secret')):
            response = self.client.post('/api/efficiency-report', json=dict(date_from='2026-10-19', date_to='2026-10-22'))
            token = response.get_json()['job_id']
            result = self.wait_job(token).get_json()
            self.assertEqual(result['status'], 'error')
            self.assertNotIn('secret', str(result))
            self.assertEqual(self.client.get('/api/efficiency-report/'+token+'/download').status_code, 409)

    def test_parallel_job_is_bounded_and_expiry_is_explicit(self):
        entered, release = threading.Event(), threading.Event()
        def loader(*args):
            entered.set(); release.wait(3)
            return build_efficiency_report([], [], [], *args, raw_rows=[])
        jobs = self.api.ReportJobs(loader)
        token = jobs.start(date(2026, 10, 19), date(2026, 10, 22))
        try:
            self.assertTrue(entered.wait(1))
            self.assertIsNone(jobs.start(date(2026, 10, 19), date(2026, 10, 22)))
        finally:
            release.set()
        self.assertIsNone(self.web.efficiency_jobs.get('missing'))
        self.assertEqual(self.client.get('/api/efficiency-report/missing').status_code, 404)

    def test_dialog_and_existing_passage_report_both_available(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        for value in ['Отчёт эффективности', 'Отчёт проходов', 'efficiencyFrom', 'efficiencyTo', 'Скопировать письмо']:
            self.assertIn(value, response.data.decode())

    def test_loader_uses_separate_readonly_sources_and_all_historical_tags(self):
        w, r, e = reference_population()
        class Cursor:
            def __init__(self, connection): self.connection = connection
            def execute(self, query, *params):
                self.connection.queries.append((query, params))
                if query.startswith('SET '): return self
                if 'dbo.RFID_Tags' in query:
                    data = ([dict(raw_reads=1600, first_read=datetime(2026, 10, 20), last_read=datetime(2026, 10, 22))]
                            if 'AS raw_reads' in query else reference_raw_reads(w, r))
                    columns = list(data[0])
                    self.description = [(key,) for key in columns]
                    self.batch = [tuple(row.get(key) for key in columns) for row in data]
                    return self
                data = w if 'dbo.Warehouse' in query else r if 'dbo.RfidTags' in query else e
                columns = list(data[0])
                self.description = [(key,) for key in columns]
                self.batch = [tuple(row.get(key) for key in columns) for row in data]
                return self
            def fetchmany(self, size):
                batch, self.batch = self.batch[:size], self.batch[size:]
                return batch
            def close(self): pass
        class Connection:
            def __init__(self): self.queries = []; self.closed = False
            def cursor(self): return Cursor(self)
            def close(self): self.closed = True
        tasks, events, reader = Connection(), Connection(), Connection()
        calls = []
        def connect(destination, **options):
            calls.append((destination, options))
            return {'task-source': tasks, 'event-source': events, 'reader-source': reader}[destination]
        base = types.SimpleNamespace(Config=types.SimpleNamespace(DB_CONN_STR='event-source', TASK_TABLE='dbo.RfidTags'),
                                     pyodbc=types.SimpleNamespace(connect=connect))
        with patch.dict('os.environ', {'KPP_TASK_CONN_STR': 'task-source', 'RFID_DB_CONNECTION': 'reader-source'}, clear=True):
            report = self.api.fetch_efficiency_report(base, date(2026, 10, 19), date(2026, 10, 22))
        self.assertEqual(report['totals']['detected'], 80)
        self.assertTrue(report['raw_checked'])
        self.assertTrue(any('dbo.RFID_Tags' in q for q, _ in reader.queries))
        self.assertFalse(any('dbo.RFID_Tags' in q for q, _ in events.queries))
        self.assertEqual(report['raw_source']['raw_reads'], 1600)
        self.assertEqual([call[0] for call in calls], ['task-source', 'event-source', 'reader-source'])
        for _, options in calls:
            self.assertTrue(options['readonly'])
            self.assertTrue(options['autocommit'])
        registry_query = next((q, p) for q, p in tasks.queries if 'dbo.RfidTags' in q)
        self.assertNotIn('Dt>=', registry_query[0])
        self.assertEqual(registry_query[1], (datetime(2026, 10, 23),))
        for connection in (tasks, events, reader):
            self.assertTrue(connection.closed)
            self.assertTrue(all(q.lstrip().startswith(('SET ', 'SELECT ')) for q, _ in connection.queries))
            self.assertNotIn('READ UNCOMMITTED', str(connection.queries))

    def test_row_limit_rejects_incomplete_population(self):
        class Cursor:
            description = [('Id',)]
            def execute(self, *_): pass
            def fetchmany(self, _): return [(1,), (2,)]
            def close(self): self.closed = True
        cursor = Cursor()
        with self.assertRaisesRegex(ValueError, 'Неполный'):
            self.api.read_rows(types.SimpleNamespace(cursor=lambda: cursor), 'SELECT Id', (), 1)
        self.assertTrue(cursor.closed)

    def test_raw_query_is_bounded_parameterized_and_failure_is_not_zero(self):
        report = build_efficiency_report(*reference_population(), date(2026, 10, 19), date(2026, 10, 22))
        with patch.object(self.api, 'read_rows', return_value=[]) as read:
            self.assertEqual(self.api.fetch_raw_evidence(object(), report, 'dbo.RFID_Tags'), [])
            self.assertTrue(read.called)
            for call in read.call_args_list:
                query, parameters = call.args[1:3]
                self.assertEqual(query.count('?'), len(parameters))
                self.assertLess(len(parameters), 2100)
                self.assertIn('r.RecordTime>=?', query)
                self.assertIn('r.RecordTime<?', query)
                self.assertNotIn('COALESCE(r.SourceReaderTime', query)
                self.assertEqual(parameters[-2:], [datetime(2026, 10, 19), datetime(2026, 10, 23)])
                self.assertNotIn('WarehouseId', query)
                self.assertIn('FirstRawId', query)
                self.assertNotIn('WAREHOUSE_ONLY', query)
        with patch.object(self.api, 'read_rows', side_effect=RuntimeError('SQL unavailable')):
            with self.assertRaises(RuntimeError):
                self.api.fetch_raw_evidence(object(), report, 'dbo.RFID_Tags')

    def test_raw_source_summary_is_required_and_uses_same_period(self):
        report = build_efficiency_report([], [], [], date(2026, 10, 19), date(2026, 10, 23))
        with patch.object(self.api, 'read_rows', return_value=[dict(raw_reads=0, first_read=None, last_read=None)]) as read:
            result = self.api.fetch_raw_summary(object(), report, 'dbo.RFID_Tags')
            self.assertEqual(result['raw_reads'], 0)
            self.assertEqual(read.call_args.args[2], (datetime(2026, 10, 19), datetime(2026, 10, 24)))
        with patch.object(self.api, 'read_rows', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'сводка'):
                self.api.fetch_raw_summary(object(), report, 'dbo.RFID_Tags')


if __name__ == '__main__':
    unittest.main()
