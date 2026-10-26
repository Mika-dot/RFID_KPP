"""Software boundary, incoming instruction order and positive log evidence."""
from datetime import date,datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest

from common.efficiency_report import build_efficiency_report,email_parts
from common.efficiency_accountability import assess_accountability
from common.efficiency_cases import attach_case_analysis,short_scorecard
from common.efficiency_communications import read_reader_communications
from common.efficiency_xlsx import generate_efficiency_xlsx
import io
import zipfile

TAG='A'*24+'B'*24


def report_one(*,request=datetime(2026,10,20,8),receipt=datetime(2026,10,20,12),
               first=datetime(2026,10,20,10),last=None,event_last=None,other_tag=False):
    registry=[dict(Id=11,Dt=request,Tag=TAG,SeriesNumber='S1',Ids='OUT-1')] if request else []
    if other_tag:registry.append(dict(Id=12,Dt=datetime(2022,1,1),Tag='C'*48,SeriesNumber='S1',Ids='OLD-OTHER-TAG'))
    raw=[dict(Tag=TAG,ReadCount=2 if last and last!=first else 1,FirstRead=first,LastRead=last or first,FirstRawId=21,LastRawId=22)] if first else []
    event=[dict(EventId=31,SourceTag=TAG,FirstSeen=first,LastSeen=event_last or last or first,
                RfidReadCount=1,IsReel=1,Task1CId=11)] if first else []
    return build_efficiency_report([dict(Id=1,Dt=receipt,SeriesNumber='S1',Ids='STORE-1')],registry,event,
                                   date(2026,10,19),date(2026,10,23),raw_rows=raw)


class InstructionOrderTests(unittest.TestCase):
    def test_new_read_after_post_warehouse_instruction_rejects_contradicted_snapshot(self):
        report=report_one(request=datetime(2026,10,20,13),first=None)
        self.assertEqual(report['details'][0]['registered_tags'],[])
        later=[dict(EPC='A'*24,TID='B'*24,Antenna=2,ReadCount=1,FirstRead='2026-10-20T14:00:00',
                    LastRead='2026-10-20T14:00:00',FirstRawId=1,LastRawId=1)]
        with self.assertRaisesRegex(ValueError,'повторно'):
            attach_case_analysis(report,later,[],[])

    def test_new_read_for_later_tag_is_checked_even_with_an_old_other_tag_instruction(self):
        report=report_one(request=datetime(2026,10,20,13),first=None,other_tag=True)
        self.assertIn('C'*48,report['details'][0]['registered_tags'])
        later=[dict(EPC='A'*24,TID='B'*24,Antenna=2,ReadCount=1,FirstRead='2026-10-20T14:00:00',
                    LastRead='2026-10-20T14:00:00',FirstRawId=1,LastRawId=1)]
        with self.assertRaisesRegex(ValueError,'повторно'):
            attach_case_analysis(report,later,[],[])

    def test_later_owner_does_not_invalidate_a_verified_tag_in_the_report_period(self):
        w=[dict(Id=1,Dt=datetime(2026,10,20,12),SeriesNumber='A')]
        r=[dict(Id=11,Dt=datetime(2026,10,20,8),Tag=TAG,SeriesNumber='A'),
           dict(Id=12,Dt=datetime(2026,10,26),Tag=TAG,SeriesNumber='B')]
        raw=[dict(Tag=TAG,ReadCount=1,FirstRead=datetime(2026,10,20,10),LastRead=datetime(2026,10,20,10),FirstRawId=1,LastRawId=1)]
        e=[dict(EventId=1,SourceTag=TAG,FirstSeen=datetime(2026,10,20,10),RfidReadCount=1,IsReel=1)]
        report=build_efficiency_report(w,r,e,date(2026,10,19),date(2026,10,23),raw_rows=raw)
        self.assertEqual(report['details'][0]['known_ambiguous_tags'],[])
        self.assertEqual(report['processing_coverage'],dict(received=1,recognized=1))

    def test_daily_bucket_uses_actual_first_post_instruction_read_not_final_period_read(self):
        w=[dict(Id=1,Dt=datetime(2026,10,20,12),SeriesNumber='S')]
        r=[dict(Id=11,Dt=datetime(2026,10,20,8),Tag=TAG,SeriesNumber='S')]
        raw=[dict(Tag=TAG,ReadCount=3,FirstRead=datetime(2026,10,19,10),LastRead=datetime(2026,10,23,10),
                  FirstEligibleRead=datetime(2026,10,20,10),FirstRawId=1,LastRawId=3)]
        e=[dict(EventId=1,SourceTag=TAG,FirstSeen=datetime(2026,10,20,10),RfidReadCount=1,IsReel=1)]
        report=build_efficiency_report(w,r,e,date(2026,10,19),date(2026,10,23),raw_rows=raw)
        self.assertEqual(next(d for d in report['software_days'] if d['date']=='2026-10-20')['received'],1)
        self.assertEqual(next(d for d in report['software_days'] if d['date']=='2026-10-23')['received'],0)
        raw[0].pop('FirstEligibleRead')
        with self.assertRaisesRegex(ValueError,'точное первое чтение'):
            build_efficiency_report(w,r,e,date(2026,10,19),date(2026,10,23),raw_rows=raw)

    def test_unchecked_processing_has_an_explicit_review_category_in_full_export(self):
        report=report_one()
        report['events_checked']=False
        report=attach_case_analysis(report,[],[],None)
        self.assertEqual(report['details'][0]['reason_code'],'PROCESSING_UNCHECKED')
        self.assertFalse(report['details'][0]['case']['raw_checked'])
        self.assertEqual(next(r[1] for r in report['details'][0]['case']['facts'] if r[0]=='Точное чтение в отчётном периоде'),1)
        self.assertEqual(short_scorecard(report)[1][3],'Не проверено')
        with zipfile.ZipFile(io.BytesIO(generate_efficiency_xlsx(report,True))) as z:
            self.assertIn('Обработка не проверена',z.read('xl/workbook.xml').decode())

    def test_old_instruction_does_not_expire_because_warehouse_or_24h_window_passed(self):
        report=report_one(request=datetime(2022,1,1))
        self.assertEqual(report['processing_coverage'],dict(received=1,recognized=1))
        self.assertEqual(report['details'][0]['control']['status'],'PROCESSED')
        self.assertFalse(report['accountability']['fulfilment_checked'])

    def test_warehouse_is_not_the_instruction_deadline_for_processing(self):
        report=report_one(receipt=datetime(2026,10,20,9),request=datetime(2026,10,20,10),first=datetime(2026,10,20,11))
        self.assertFalse(report['details'][0]['has_rfid'])  # legacy storage diagnostic
        self.assertEqual(report['processing_coverage'],dict(received=1,recognized=1))
        self.assertTrue(report['details'][0]['control']['request_after_warehouse'])
        self.assertEqual(report['accountability']['late_transfers'],1)

    def test_order_after_passage_and_storage_is_not_a_software_processing_failure(self):
        report=report_one(first=datetime(2026,10,20,9),receipt=datetime(2026,10,20,10),request=datetime(2026,10,20,11))
        c=report['details'][0]['control']
        self.assertEqual(c['status'],'AFTER_READ')
        self.assertTrue(c['late_initial_passage'])
        self.assertTrue(c['request_after_warehouse'])
        self.assertEqual(report['processing_coverage'],dict(received=0,recognized=0))
        self.assertEqual(c['late_first_reads'][0]['request_ids'],['11'])
        self.assertEqual(report['accountability']['late_transfers'],1)  # no double count
        self.assertNotIn('100%',email_parts(report)['text'].split('RFID КПП:')[1].split('\n')[0])

    def test_earlier_instruction_for_a_different_full_tag_does_not_authorize_target_tag(self):
        report=report_one(request=datetime(2026,10,20,11),first=datetime(2026,10,20,10),other_tag=True)
        self.assertEqual(report['details'][0]['control']['status'],'AFTER_READ')
        self.assertEqual(report['processing_coverage']['received'],0)

    def test_recognized_event_before_instruction_does_not_prove_processing_after_it(self):
        report=report_one(request=datetime(2026,10,20,11),first=datetime(2026,10,20,9),
                          last=datetime(2026,10,20,12),event_last=datetime(2026,10,20,10))
        self.assertEqual(report['processing_coverage'],dict(received=1,recognized=0))
        self.assertEqual(report['accountability']['processing_review'],1)

    def test_processing_can_resume_after_late_instruction_without_erasing_first_read_fact(self):
        report=report_one(request=datetime(2026,10,20,11),first=datetime(2026,10,20,9),last=datetime(2026,10,20,12))
        self.assertEqual(report['processing_coverage'],dict(received=1,recognized=1))
        self.assertTrue(report['details'][0]['control']['late_initial_passage'])
        self.assertEqual(report['request_evidence'][0]['received_at'],'2026-10-20T11:00:00')

    def test_old_history_does_not_become_the_passage_of_the_selected_period(self):
        report=report_one(first=None)
        history=[dict(EPC='A'*24,TID='B'*24,Antenna=2,ReadCount=1,FirstRead='2022-01-01T10:00:00',
                      LastRead='2022-01-01T10:00:00',FirstRawId=1,LastRawId=1)]
        report=attach_case_analysis(report,history,[],[])
        self.assertEqual(report['accountability']['late_transfers'],0)
        self.assertEqual(report['details'][0]['control']['status'],'NO_INPUT')

    def test_49_available_48_recognized_is_97_96_not_warehouse_read_rate(self):
        warehouse=[];registry=[];raw=[];events=[]
        for i in range(167):
            series=f'S{i}';tag=f'{i:048X}'
            warehouse.append(dict(Id=i,Dt=datetime(2026,10,20,12),SeriesNumber=series))
            if i<112:registry.append(dict(Id=i,Dt=datetime(2026,10,20,8),SeriesNumber=series,Tag=tag))
            if i<49:
                raw.append(dict(Tag=tag,ReadCount=1,FirstRead=datetime(2026,10,20,10),LastRead=datetime(2026,10,20,10),FirstRawId=i+1,LastRawId=i+1))
                events.append(dict(EventId=i,SourceTag=tag,FirstSeen=datetime(2026,10,20,10),RfidReadCount=1,IsReel=int(i<48)))
        report=build_efficiency_report(warehouse,registry,events,date(2026,10,19),date(2026,10,23),raw_rows=raw)
        text=email_parts(report)['text']
        self.assertIn('48/49 — 97,96%',text)
        self.assertNotIn('43,75%',text)
        self.assertEqual(report['accountability']['unresolved_no_input'],63)
        self.assertEqual(report['accountability']['no_instruction'],55)
        self.assertEqual(report['accountability']['processing_review'],1)
        self.assertEqual(short_scorecard(report)[1][1:4],[48,49,'97,96%'])


class ReaderLogEvidenceTests(unittest.TestCase):
    period=dict(date_from='2026-10-19',date_to='2026-10-23')
    connected='RFID reader connected, epoch=11111111-1111-1111-1111-111111111111'

    def read(self,content,**kwargs):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,'RfidReader.log').write_text(content,encoding='utf-8')
            return read_reader_communications(self.period,folder,local_zone=timezone.utc,**kwargs)

    def test_failed_attempts_then_reconnect_are_scoped_local_observations(self):
        data=self.read('2026-10-20 10:00:01 [ERROR] TCPConnect code=-1\n'
                       '2026-10-20 10:00:06 [ERROR] TCPConnect code=-1\n'
                       '2026-10-20 10:00:11 [INFO] '+self.connected+'\n')
        self.assertTrue(data['checked']);self.assertFalse(data['complete'])
        self.assertEqual(data['intervals'][0]['duration_sec'],10)
        self.assertEqual(data['events'][0]['at'],'2026-10-20T13:00:01')
        self.assertEqual(data['events'][0]['source'],'RfidReader.log:1')

    def test_trace_payload_never_leaks_and_reader_exception_is_not_classified_as_network(self):
        data=self.read('2026-10-20 10:00:01 [ERROR] RFID connection epoch failed\n'
                       'Traceback PWD=secret\n2026-10-20 10:00:06 [INFO] '+self.connected+'\n')
        self.assertNotIn('secret',json.dumps(data))
        self.assertEqual(data['events'][0]['kind'],'READER_EXCEPTION')
        self.assertEqual(data['intervals'],[])

    def test_gap_or_service_restart_does_not_create_an_invented_long_outage(self):
        data=self.read('2026-10-20 10:00:01 [ERROR] TCPConnect code=-1\n'
                       '2026-10-22 10:00:11 [INFO] '+self.connected+'\n')
        self.assertEqual(data['intervals'],[])
        self.assertEqual(len(data['events']),2)

    def test_missing_or_oversized_log_is_unmeasured(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(read_reader_communications(self.period,folder)['checked'])
        data=self.read('2026-10-20 10:00:01 [ERROR] TCPConnect code=-1\n',max_bytes=10)
        self.assertFalse(data['checked']);self.assertEqual(data['events'],[])


if __name__=='__main__':unittest.main()
