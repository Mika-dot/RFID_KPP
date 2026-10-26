"""Reproduce the server report from private CSV exports; never contacts SQL."""
import argparse
from collections import Counter
import csv
from datetime import date, datetime, timedelta
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.efficiency_report import build_efficiency_report, tag_key, validate_period
from common.efficiency_diagnostics import reconcile
from common.efficiency_xlsx import generate_efficiency_xlsx


def rows(folder, table, required=True):
    # The underscore is significant: RFID_Tags is Reader, RfidTags is 1C.
    matches = sorted(p for p in folder.glob(table+'_*.csv')
                     if re.fullmatch(re.escape(table)+r'_\d{12,14}\.csv',p.name))
    if not matches:
        if required:
            raise ValueError('Нет CSV таблицы '+table)
        return None
    if len(matches) != 1:
        raise ValueError('Оставьте одну выгрузку таблицы '+table)
    with matches[0].open(encoding='utf-8-sig', newline='') as source:
        return list(csv.DictReader(source))


def audit(folder, first, last):
    validate_period(first,last)
    start, end = first.isoformat(), (last+timedelta(days=1)).isoformat()
    warehouse, registry = rows(folder,'Warehouse'), rows(folder,'RfidTags')
    for records in (warehouse,registry):
        for row in records:
            row['Dt'] = datetime.fromisoformat(row['Dt']) if row['Dt'] else None
    events = rows(folder,'KPP_ReelEvents',False)
    for row in events or ():
        row['WarehouseId'] = row.get('WarehouseId') or None
    state = rows(folder,'KPP_RuntimeState',False)
    cursor = next((int(row['StateValue']) for row in state or ()
                   if row['StateKey']=='LAST_RFID_ID_V3' and row['StateValue'].isdigit()),None)
    period, history, epc_counts = {}, {}, Counter()
    total = consumed = 0
    delivery=dict(checked=True,total=0,timed_rows=0,late_rows=0,after_reconnect=0,max_delay_sec=None,threshold_sec=120)
    paths = list(folder.glob('RFID_Tags_*.csv'))
    if len(paths) != 1:
        raise ValueError('Требуется одна полная выгрузка RFID_Tags')
    with paths[0].open(encoding='utf-8-sig',newline='') as source:
        for row in csv.DictReader(source):
            tag=tag_key(row['EPC'])+tag_key(row['TID'])
            at=row['RecordTime']; identity=int(row['Id'])
            for collection in (history, period) if start<=at<end else (history,):
                if tag not in collection:
                    collection[tag]=dict(Tag=tag,ReadCount=0,FirstRead=at,LastRead=at,FirstRawId=identity,LastRawId=identity)
                item=collection[tag];item['ReadCount']+=1
                item['FirstRead']=min(item['FirstRead'],at);item['LastRead']=max(item['LastRead'],at)
                item['FirstRawId']=min(item['FirstRawId'],identity);item['LastRawId']=max(item['LastRawId'],identity)
            if start<=at<end:
                total+=1;consumed+=int(cursor is not None and identity<=cursor)
                epc_counts[tag_key(row['EPC'])]+=1
                delivery['total']+=1
                quality=row.get('TimeQuality') or ''
                delivery['after_reconnect']+=int(quality=='APPROXIMATE_AFTER_RECONNECT')
                if row.get('ReceivedAt') and row.get('SourceReaderTime') and quality in ('HOST_CAPTURE_TIME','APPROXIMATE_AFTER_RECONNECT'):
                    received,source_time=datetime.fromisoformat(row['ReceivedAt']),datetime.fromisoformat(row['SourceReaderTime'])
                    lag=(received-source_time).total_seconds()
                    if lag>=0:
                        delivery['timed_rows']+=1;delivery['late_rows']+=int(lag>120)
                        seconds=int((received.replace(microsecond=0)-source_time.replace(microsecond=0)).total_seconds())
                        delivery['max_delay_sec']=max(delivery['max_delay_sec'] or 0,seconds)
    report=build_efficiency_report(warehouse,registry,events or (),first,last,raw_rows=list(period.values()))
    report['events_checked']=events is not None
    report['raw_source']=dict(raw_reads=total)
    report['delivery']=delivery
    proofs=[]
    for tag in sorted({tag for row in report['details'] if row['has_rfid'] and not row['detected']
                       for tag in set(row['registered_tags'])-set(row['ambiguous_tags'])}):
        item=dict(history.get(tag,dict(Tag=tag,ReadCount=0,FirstRead=None,LastRead=None,FirstRawId=None,LastRawId=None)))
        item.update(PeriodReads=period.get(tag,{}).get('ReadCount',0),EpcPeriodReads=epc_counts[tag[:24]])
        proofs.append(item)
    errors=rows(folder,'KPP_ProcessingErrors',False)
    processing=dict(checked=errors is not None)
    if errors is not None:
        selected=[row for row in errors if start<=row['CreatedAt']<end]
        processing.update(total=len(selected),unresolved=sum(not row['ResolvedAt'] for row in selected))
    if cursor is not None:
        processing.update(cursor=cursor,pipeline_checked=True,read_total=total,consumed=consumed)
    return reconcile(report,proofs,processing=processing)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--date-from',type=date.fromisoformat,required=True)
    parser.add_argument('--date-to',type=date.fromisoformat,required=True)
    parser.add_argument('--xlsx',type=Path)
    parser.add_argument('--json',type=Path)
    args=parser.parse_args()
    report=audit(args.data_dir,args.date_from,args.date_to)
    if args.xlsx:
        args.xlsx.parent.mkdir(parents=True,exist_ok=True)
        args.xlsx.write_bytes(generate_efficiency_xlsx(report,True))
    if args.json:
        args.json.parent.mkdir(parents=True,exist_ok=True)
        args.json.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(totals=report['totals'],processing=report['processing_coverage'],
                         reconciliation=report['reconciliation']),ensure_ascii=False))


if __name__=='__main__':main()
