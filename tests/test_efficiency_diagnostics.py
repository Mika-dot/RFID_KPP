from datetime import date, datetime
import importlib
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

from common.efficiency_report import build_efficiency_report, email_parts
from common.efficiency_diagnostics import reconcile, operational_rows, verification_text


class ReconciliationTests(unittest.TestCase):
    def report(self):
        w = [dict(Id=i, Dt=datetime(2026,10,20,12), SeriesNumber=f'S{i}') for i in range(1,7)]
        r = [dict(Id=i, Dt=datetime(2026,5,1), SeriesNumber=f'S{i}', Tag=f'{i:048X}') for i in range(1,6)]
        raw = [dict(Tag=r[0]['Tag'], ReadCount=10, FirstRead=datetime(2026,10,20), LastRead=datetime(2026,10,20), FirstRawId=11, LastRawId=20)]
        events = [dict(EventId=99, SourceTag=r[0]['Tag'], FirstSeen=datetime(2026,10,20), RfidReadCount=10,
                       WarehouseId=999, IsReel=1, FinalDirection='IN')]
        return build_efficiency_report(w,r,events,date(2026,10,19),date(2026,10,23),raw_rows=raw)

    def history(self, identity, **change):
        row = dict(Tag=f'{identity:048X}',ReadCount=0,PeriodReads=0,EpcPeriodReads=0,
                   FirstRead=None,LastRead=None,FirstRawId=None,LastRawId=None)
        row.update(change)
        return row

    def test_recognition_is_separate_from_receipt_link_and_direction(self):
        report = self.report()
        self.assertEqual(report['diagnostics']['kpp_confirmed'], 0)
        self.assertEqual(report['processing_coverage'], dict(received=1, recognized=1))
        self.assertEqual(report['totals']['detected'], 1)

    def test_unknown_or_warehouse_only_event_is_not_successful_recognition(self):
        report=self.report()
        for row in report['details']:
            if row['aggregator_tags']:
                self.assertEqual(row['aggregator_event_ids'],['99'])
        # Receipt linkage is a separate diagnostic; a missing event is not
        # fabricated simply because the Reader has a real raw proof.
        w=[dict(Id=1,Dt=datetime(2026,10,20),SeriesNumber='X')]
        r=[dict(Dt=datetime(2026,5,1),SeriesNumber='X',Tag='A'*48)]
        raw=[dict(Tag='A'*48,ReadCount=1,FirstRead=datetime(2026,10,20),LastRead=datetime(2026,10,20),FirstRawId=1,LastRawId=1)]
        for changes in [dict(IsReel=0),dict(IsReel=1,SessionCloseReason='WAREHOUSE_ONLY')]:
            event=dict(EventId=1,SourceTag='A'*48,FirstSeen=datetime(2026,10,20),RfidReadCount=1,**changes)
            result=build_efficiency_report(w,r,[event],date(2026,10,19),date(2026,10,23),raw_rows=raw)
            self.assertEqual(result['processing_coverage'],dict(received=1,recognized=0))
            result=reconcile(result)
            self.assertEqual(result['details'][0]['reason_code'],'READ_UNCLASSIFIED')
            self.assertEqual(result['reconciliation']['unrecognized_series'],['X'])

    def test_recognized_session_can_span_midnight_or_start_days_before_period(self):
        w=[dict(Id=1,Dt=datetime(2026,10,20),SeriesNumber='X')]
        r=[dict(Dt=datetime(2026,5,1),SeriesNumber='X',Tag='A'*48)]
        raw=[dict(Tag='A'*48,ReadCount=1,FirstRead=datetime(2026,10,19),LastRead=datetime(2026,10,19),FirstRawId=1,LastRawId=1)]
        for first in (datetime(2026,10,18,23,59),datetime(2026,10,15)):
            for last,expected in ((datetime(2026,10,19,0,1),1),(datetime(2026,10,18,23,59),0)):
                event=dict(EventId=1,SourceTag='A'*48,FirstSeen=first,LastSeen=last,RfidReadCount=1,IsReel=1)
                result=build_efficiency_report(w,r,[event],date(2026,10,19),date(2026,10,23),raw_rows=raw)
                self.assertEqual(result['processing_coverage']['recognized'],expected)

    def test_history_reasons_are_evidence_not_assigned_blame(self):
        history = [self.history(2, ReadCount=2, FirstRead='2026-05-01T10:00:00',LastRead='2026-05-01T11:00:00',FirstRawId=1,LastRawId=2),
                   self.history(3,EpcPeriodReads=4),self.history(4)]
        report = reconcile(self.report(),history,processing=dict(checked=True,total=0,unresolved=0))
        self.assertEqual([x['reason_code'] for x in report['details']],
                         ['READ','OUTSIDE_PERIOD','EPC_ONLY','NOT_IN_HISTORY','NOT_CHECKED','NO_TAG'])
        self.assertEqual(sum(n for _,n in report['reconciliation']['counts']),report['totals']['missed'])
        self.assertIn('не устанавливает физическую причину',verification_text(report))
        rows = operational_rows(report)
        self.assertIn('Доля потерь не установлена',str(rows))
        self.assertIn('требуют проверки',str(rows))

    def test_history_error_cannot_become_no_reads_in_history(self):
        report = reconcile(self.report(),None)
        self.assertEqual(report['details'][1]['reason_code'],'NOT_CHECKED')
        self.assertFalse(report['reconciliation']['history_checked'])
        self.assertEqual(report['totals']['detected'],1)

    def test_late_read_contradicting_numerator_requires_new_snapshot(self):
        for row in [self.history(2,PeriodReads=1),self.history(2,ReadCount=1,
                    FirstRead='2026-10-20T10:00:00',LastRead='2026-10-20T10:00:00')]:
            with self.subTest(row=row),self.assertRaisesRegex(ValueError,'повторно'):
                reconcile(self.report(),[row])

    def test_processing_percentage_is_scoped_and_not_assumed_from_zero_errors(self):
        report=reconcile(self.report(),[],processing=dict(checked=True,total=0,unresolved=0))
        self.assertEqual(operational_rows(report)[1][-1],'Не измерено')
        report['reconciliation']['processing'].update(pipeline_checked=True,consumed=7,read_total=10)
        self.assertEqual(operational_rows(report)[1][-1],'70%')

    def test_director_letter_is_short_and_omits_diagnostic_dump(self):
        text=email_parts(reconcile(self.report()))['text']
        self.assertIn('Распознавание полученных RFID',text)
        self.assertIn('1/1 — 100%',text)
        self.assertNotIn('FirstRawId',text)
        self.assertNotIn('Сырых чтений',text)
        self.assertNotIn('Дата\tВсего барабанов',text)
        self.assertNotIn('000000000000000000000000000000000000000000000001',text)
        self.assertLess(len(text.split()),150)


class DiagnosticSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
        try:cls.api=importlib.import_module('efficiency_api')
        finally:sys.path.pop(0)

    def test_history_queries_use_indexable_epc_and_full_tid(self):
        report=ReconciliationTests().report()
        with patch.object(self.api,'read_rows',return_value=[]) as read:
            self.api.fetch_raw_history(object(),report,'dbo.RFID_Tags')
            query,params=read.call_args.args[1:3]
            self.assertIn('r.EPC=v.EPC',query)
            self.assertIn('r.TID=v.TID',query)
            self.assertNotIn('UPPER(r.EPC)',query)
            self.assertEqual(query.count('?'),len(params))
            self.assertLess(len(params),2100)

    def test_cursor_missing_does_not_erase_successfully_checked_error_log(self):
        with patch.object(self.api,'read_rows',side_effect=[[dict(total=0,unresolved=0)],RuntimeError('PWD=secret')]):
            with self.assertLogs('perimeter.efficiency-report',level='WARNING') as logs:
                result=self.api.fetch_processing_summary(object(),datetime(2026,10,19),datetime(2026,10,24))
            self.assertTrue(result['checked'])
            self.assertNotIn('cursor',result)
            self.assertNotIn('secret',str(logs.output))

    def test_raw_progress_counts_actual_rows_not_id_distance(self):
        report=ReconciliationTests().report()
        with patch.object(self.api,'read_rows',return_value=[dict(read_total=10,consumed=7)]) as read:
            result=self.api.fetch_raw_progress(object(),report,'dbo.RFID_Tags',1000)
            self.assertEqual(result,dict(pipeline_checked=True,read_total=10,consumed=7))
            self.assertEqual(read.call_args.args[2],(1000,datetime(2026,10,19),datetime(2026,10,24)))


if __name__=='__main__':unittest.main()
