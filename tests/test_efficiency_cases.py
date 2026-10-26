"""Chronology, causality and native graph regression with synthetic identities."""
from datetime import date, datetime
import importlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from common.efficiency_report import build_efficiency_report
from common.efficiency_diagnostics import reconcile
from common.efficiency_cases import attach_case_analysis, short_scorecard, activity_window
from common.efficiency_xlsx import generate_efficiency_xlsx


TAG='A'*24+'B'*24


def population(*, registered=datetime(2026,10,20,8), read=None, recognized=False):
    w=[dict(Id=1,Dt=datetime(2026,10,20,12),SeriesNumber='S1')]
    r=[dict(Id=11,Dt=registered,Tag=TAG,SeriesNumber='S1')]
    raw=[dict(Tag=TAG,ReadCount=1,FirstRead=read,LastRead=read,FirstRawId=21,LastRawId=21)] if read else []
    events=[dict(EventId=31,SourceTag=TAG,FirstSeen=read,LastSeen=read,RfidReadCount=1,
                 IsReel=int(recognized),FinalDirection='OUT',ReelClassification='CONFIRMED' if recognized else 'NOT_CONFIRMED')] if read else []
    return reconcile(build_efficiency_report(w,r,events,date(2026,10,19),date(2026,10,23),raw_rows=raw)),events


def raw_group(*,tid='B'*24,at='2026-10-20T10:00:00',count=1,**changes):
    row=dict(EPC='A'*24,TID=tid,Antenna=2,ReadCount=count,FirstRead=at,LastRead=at,
             FirstRawId=21,LastRawId=21,FirstReceived='2026-10-20T10:00:01',LastReceived='2026-10-20T10:00:01',
             TimedRows=count,LateRows=0)
    row.update(changes)
    return row


class CaseEvidenceTests(unittest.TestCase):
    def test_unchecked_recognition_status_survives_xlsx_percentage_formulas(self):
        report,events=population(read=datetime(2026,10,20,10),recognized=False)
        report['events_checked']=False
        report=attach_case_analysis(report,[raw_group()],[],None)
        ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with zipfile.ZipFile(io.BytesIO(generate_efficiency_xlsx(report))) as z:
            sheet=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            cells={c.attrib['r']:c for c in sheet.findall('.//m:c',ns)}
            self.assertIsNone(cells['E6'].find('m:f',ns))
            self.assertEqual(cells['E6'].find('m:is/m:t',ns).text,'Не проверено')
            self.assertEqual(cells['C6'].find('m:is/m:t',ns).text,'—')
            self.assertIsNotNone(cells['E5'].find('m:f',ns))

    def test_fractional_receipt_uses_identical_fact_and_chart_activity_buckets(self):
        report,events=population()
        row=report['details'][0]
        row['warehouse_dt']='2026-10-20T12:30:00'
        activity=[dict(Hour='2026-10-20T00:00:00',Antenna=2,ReadCount=2),
                  dict(Hour='2026-10-21T00:00:00',Antenna=2,ReadCount=3),
                  dict(Hour='2026-10-21T01:00:00',Antenna=2,ReadCount=99)]
        self.assertEqual(activity_window(row['warehouse_dt']),
                         (datetime(2026,10,20),datetime(2026,10,21,1)))
        report=attach_case_analysis(report,[],activity,events)
        fact=next(r for r in report['details'][0]['case']['facts'] if r[0].startswith('Другие чтения:'))
        self.assertEqual(fact[1],5)
        ns={'c':'http://schemas.openxmlformats.org/drawingml/2006/chart'}
        with zipfile.ZipFile(io.BytesIO(generate_efficiency_xlsx(report,True))) as z:
            plots=[ET.fromstring(z.read(p)) for p in z.namelist() if re_chart(p)]
            chart=next(p for p in plots if p.find('.//c:lineChart',ns) is not None)
            values=[int(p.text) for p in chart.findall('.//c:val/c:numRef/c:numCache/c:pt/c:v',ns)]
            self.assertEqual(len(values),25)
            self.assertEqual(sum(values),fact[1])

    def test_other_tid_is_not_claimed_as_missing_tid_or_defective_tag(self):
        report,events=population()
        report=attach_case_analysis(report,[raw_group(tid='C'*24,count=4)],[],events)
        facts=dict((r[0],r[1]) for r in report['details'][0]['case']['facts'])
        self.assertEqual(facts['Тот же EPC, TID отсутствует'],0)
        self.assertEqual(facts['Тот же EPC, другой TID'],4)
        self.assertEqual(report['totals']['detected'],0)
        self.assertIn('другой катушке',str(report['details'][0]['case']['checks']))

    def test_empty_tid_is_separate_from_other_tid(self):
        report,events=population()
        report=attach_case_analysis(report,[raw_group(tid='',count=3),raw_group(tid='C'*24,count=4)],[],events)
        facts=dict((r[0],r[1]) for r in report['details'][0]['case']['facts'])
        self.assertEqual((facts['Тот же EPC, TID отсутствует'],facts['Тот же EPC, другой TID']),(3,4))

    def test_late_incoming_request_is_proven_by_confirmed_timestamp_contract(self):
        report,events=population(registered=datetime(2026,10,26,9),read=datetime(2026,10,20,10))
        self.assertEqual(report['totals']['tagged'],0)
        report=attach_case_analysis(report,[raw_group()],[],events)
        self.assertEqual(report['one_c']['later_binding'],1)
        self.assertEqual(report['one_c']['document_after_read'],1)
        self.assertTrue(report['one_c']['arrival_checked'])
        case=report['details'][0]['case']
        self.assertEqual(case['late_documents'][0]['document'],'2026-10-26T09:00:00')
        self.assertIn('до передачи КПП не мог знать',str(case['checks']))
        self.assertEqual(report['details'][0]['control']['status'],'AFTER_READ')
        self.assertEqual(report['processing_coverage']['received'],0)
        self.assertEqual(report['totals']['detected'],0)

    def test_outside_reads_show_before_and_after_with_real_event_direction(self):
        report,events=population()
        report=reconcile(report,[dict(Tag=TAG,ReadCount=2,PeriodReads=0,EpcPeriodReads=0,
                                     FirstRead='2026-10-18T23:00:00',LastRead='2026-10-26T09:00:00',FirstRawId=1,LastRawId=99)])
        events=[dict(EventId=90,SourceTag=TAG,FirstSeen='2026-10-26T09:00:00',RfidReadCount=1,IsReel=1,FinalDirection='OUT')]
        report=attach_case_analysis(report,[raw_group(at='2026-10-18T23:00:00'),raw_group(at='2026-10-26T09:00:00')],[],events)
        case=report['details'][0]['case']
        stages=[p['stage'] for p in case['timeline']]
        self.assertIn('Точное чтение до периода',stages)
        self.assertIn('Точное чтение после периода',stages)
        self.assertIn('направление=OUT',str(case['timeline']))
        self.assertEqual(report['totals']['detected'],0)

    def test_optional_history_failure_is_unmeasured_not_zero(self):
        report,events=population()
        report=attach_case_analysis(report,None,None,None)
        case=report['details'][0]['case']
        self.assertFalse(case['raw_checked'])
        self.assertIn('Не проверено полностью',str(case['facts']))
        self.assertFalse(report['case_sources']['activity_checked'])

    def test_noncanonical_tag_does_not_claim_complete_epc_history_check(self):
        report,events=population()
        report['details'][0]['registered_tags']=['X'*48]
        report['details'][0]['registrations']=[]
        report=attach_case_analysis(report,[],[],events)
        self.assertFalse(report['details'][0]['case']['raw_checked'])

    def test_case_query_new_read_cannot_contradict_old_main_snapshot(self):
        report,events=population()
        with self.assertRaisesRegex(ValueError,'повторно'):
            attach_case_analysis(report,[raw_group()],[],events)

    def test_scorecard_separates_automation_from_read_coverage_and_network(self):
        report,events=population(read=datetime(2026,10,20,10),recognized=False)
        report=attach_case_analysis(report,[raw_group()],[],events)
        rows=short_scorecard(report)
        self.assertEqual(rows[1][1:4],[0,1,'0%'])
        self.assertEqual(report['accountability']['processing_review'],1)
        self.assertEqual(next(r for r in rows if r[0]=='Связь со считывателем')[3],'Доля отказа не измерена')

    def test_xlsx_all_series_cases_graph_sources_navigation_and_no_letter(self):
        report,events=population(read=datetime(2026,10,20,10))
        report=attach_case_analysis(report,[raw_group()],
                                    [dict(Hour='2026-10-20T10:00:00',Antenna=2,ReadCount=3)],events)
        ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with zipfile.ZipFile(io.BytesIO(generate_efficiency_xlsx(report,True))) as z:
            for name in z.namelist():
                ET.fromstring(z.read(name))
            names=[s.attrib['name'] for s in ET.fromstring(z.read('xl/workbook.xml')).findall('m:sheets/m:sheet',ns)]
            self.assertIn('001_S1',names)
            main=z.read('xl/worksheets/sheet1.xml').decode()
            self.assertNotIn('ТЕКСТ ПИСЬМА',main)
            self.assertNotIn('Поскольку RFID',main)
            self.assertIn('fitToHeight="1"',main)
            detail=ET.fromstring(z.read('xl/worksheets/sheet3.xml'))
            self.assertEqual(len(detail.findall('m:sheetData/m:row',ns)),2)
            charts=[p for p in z.namelist() if re_chart(p)]
            self.assertEqual(len(charts),4)  # daily, category, two individual plots
            self.assertIn("'001_S1'!",z.read(charts[-1]).decode())
            index=names.index('001_S1')+1
            case=z.read(f'xl/worksheets/sheet{index}.xml').decode()
            self.assertIn('KPP_ReelEvents.EventId=31',case)
            self.assertIn('NOT_CONFIRMED',case)
            self.assertIn('location="\'Проверка катушек\'!F2"',case)


def re_chart(path):
    return path.startswith('xl/charts/chart') and path.endswith('.xml')


class CaseSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
        try: cls.api=importlib.import_module('efficiency_api')
        finally: sys.path.pop(0)

    def test_case_queries_use_epc_index_and_keep_tid_variants(self):
        report,_=population()
        with patch.object(self.api,'read_rows',return_value=[]) as read:
            self.api.fetch_case_raw(object(),report,'dbo.RFID_Tags')
            query,params=read.call_args.args[1:3]
            self.assertIn('r.EPC=v.EPC',query)
            self.assertIn('GROUP BY r.EPC,r.TID,r.Antenna',query)
            self.assertEqual(query.count('?'),len(params))
            self.assertLess(len(params),2100)
            self.assertNotIn('UPPER(r.EPC)',query)

    def test_raw_fetch_keeps_tags_required_by_legacy_history_even_if_control_is_ambiguous(self):
        report,_=population()
        report['details'][0]['known_ambiguous_tags']=[TAG]
        with patch.object(self.api,'read_rows',return_value=[]) as read:
            self.api.fetch_raw_evidence(object(),report,'dbo.RFID_Tags')
            self.assertIn(TAG,read.call_args.args[2])

    def test_activity_is_bounded_counts_and_includes_receipt_context_outside_period(self):
        report,_=population()
        with patch.object(self.api,'read_rows',return_value=[]) as read:
            self.api.fetch_reader_activity(object(),report,'dbo.RFID_Tags')
            query,params=read.call_args.args[1:3]
            self.assertEqual(params,(datetime(2026,10,18,12),datetime(2026,10,24,12)))
            self.assertIn('RecordTime>=? AND RecordTime<?',query)
            self.assertIn('Antenna',query)

    def test_event_history_seeks_tags_and_retains_known_spanning_events(self):
        report,_=population()
        known=[dict(EventId=99)]
        with patch.object(self.api,'read_rows',return_value=[]) as read:
            result=self.api.fetch_case_events(object(),report,'audit.ReelEvents',known)
            self.assertEqual(result,known)
            query,params=read.call_args.args[1:3]
            self.assertIn('FROM audit.ReelEvents',query)
            self.assertIn('WHERE SourceTag IN (?)',query)
            self.assertIn('ReelClassification',query)
            self.assertEqual(params,[TAG])


if __name__=='__main__':unittest.main()
