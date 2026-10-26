"""KPP software boundary and incoming 1C instructions, not warehouse deadlines.

Business contract confirmed by the owner on 2026-10-26: RfidTags.Dt is the
time 1C transmitted the instruction; it has no age-based expiry and remains
valid until fulfilled. The source has no explicit fulfilment/closure field:
an old matching event alone must not silently cancel an instruction.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from common.efficiency_report import local_time, tag_key


CONTROL_STATUS = {
    'PROCESSED': 'Входные данные получены, катушка распознана',
    'PROCESSING_REVIEW': 'Чтение и заявка есть; распознавание требует разбора',
    'EVENTS_UNCHECKED': 'Чтение и заявка есть; события обработки не проверены',
    'NO_INSTRUCTION': 'Распорядка 1С отсутствует в доступном реестре',
    'AFTER_READ': 'Распорядка 1С поступила после доступных чтений',
    'AFTER_WAREHOUSE': 'Первая распорядка 1С поступила после складской записи',
    'AMBIGUOUS': 'Принадлежность полной метки неоднозначна',
    'NO_INPUT': 'Заявка 1С есть; исходное чтение периода не поступило',
    'RAW_UNCHECKED': 'Исходный журнал RFID не проверен',
}


def assess_accountability(report):
    report = dict(report)
    details, counts, requests = [], Counter(), []
    received = recognized = late_first = 0
    days = defaultdict(lambda: dict(received=0, recognized=0))
    for original in report['details']:
        row = dict(original)
        regs = [dict(r) for r in row.get('registrations', ())]
        by_tag = defaultdict(list)
        for r in regs:
            r['tag'] = tag_key(r['tag'])
            by_tag[r['tag']].append(r)
        proofs = list(row.get('all_raw_evidence', row.get('raw_evidence', ())))
        proofs = [p for p in proofs if p['tag'] not in row.get('known_ambiguous_tags', row['ambiguous_tags'])]
        actionable, late = set(), []
        first_read = min((p['first_read'] for p in proofs), default=None)
        last_read = max((p['last_read'] for p in proofs), default=None)
        first_instruction = min((r['dt'] for r in regs), default=None)
        request_ready_at_first = bool(first_read and any(
            r['dt']<=first_read for p in proofs if p['first_read']==first_read for r in by_tag[p['tag']]))
        eligible_at = []
        for p in proofs:
            tag = p['tag']
            at = min((r['dt'] for r in by_tag[tag]), default=None)
            if at is None:
                continue
            if at <= p['last_read']:
                actionable.add(tag)
                eligible=p.get('first_eligible_read') or (p['first_read'] if at<=p['first_read'] else None)
                if eligible is None:
                    raise ValueError('Не проверено первое чтение после распорядки')
                eligible_at.append(eligible)
            if p['first_read'] < at:
                late.append(dict(tag=tag,read_at=p['first_read'],request_at=at,
                                 raw_id_from=p['first_id'],raw_id_to=p['last_id'],
                                 request_ids=[r['id'] for r in by_tag[tag] if r['dt']==at]))
        recognized_tags = actionable & set(row.get('known_aggregator_tags', row.get('aggregator_tags', ())))
        if 'known_aggregator_evidence' in row:
            # An earlier recognized event cannot prove processing after a late
            # incoming instruction. LastSeen is the last physical read in it.
            recognized_tags={tag for tag in recognized_tags if any(
                local_time(e['last'])>=min(local_time(r['dt']) for r in by_tag[tag])
                for e in row['known_aggregator_evidence'].get(tag,()))}
        has_input, success = bool(actionable), bool(recognized_tags)
        after_storage = bool(first_instruction and first_instruction > row['warehouse_dt'])
        if not report.get('raw_checked'):
            status='RAW_UNCHECKED'
        elif not regs:
            status='NO_INSTRUCTION'
        elif has_input:
            status=('PROCESSED' if success else 'PROCESSING_REVIEW') if report.get('events_checked',True) else 'EVENTS_UNCHECKED'
        elif proofs:
            status='AFTER_READ'
        elif after_storage:
            status='AFTER_WAREHOUSE'
        elif row.get('known_ambiguous_tags') and (not (set(by_tag)-set(row['known_ambiguous_tags']))
                 or bool(set(row.get('raw_matched_tags',())) & set(row['known_ambiguous_tags']))):
            status='AMBIGUOUS'
        else:
            status='NO_INPUT'
        counts[status]+=1
        received+=int(has_input)
        recognized+=int(success)
        late_first+=int(bool(late and not request_ready_at_first))
        if has_input:
            bucket=days[min(eligible_at)[:10]]
            bucket['received']+=1;bucket['recognized']+=int(success)
        row['control'] = dict(status=status,result=CONTROL_STATUS[status],
                              first_read=first_read,last_read=last_read,first_instruction=first_instruction,
                              first_eligible_read=min(eligible_at) if eligible_at else None,
                              actionable_tags=sorted(actionable),recognized_tags=sorted(recognized_tags),
                              late_first_reads=late,request_after_warehouse=after_storage,
                              late_initial_passage=bool(late and not request_ready_at_first),
                              received=has_input,recognized=success,
                              activity_anchor=first_read or row['warehouse_dt'])
        # A later instruction after the storage record is a process-order fact,
        # not a claim that Warehouse.Dt is the physical passage timestamp.
        row['control']['storage_sequence'] = ('Заявка после складской записи' if after_storage else
                                             'Заявка не позже складской записи' if regs else 'Заявки в реестре нет')
        for r in regs:
            same=[p for p in proofs if p['tag']==r['tag']]
            first=min((p['first_read'] for p in same),default=None)
            last=max((p['last_read'] for p in same),default=None)
            requests.append(dict(series=row['series'],id=r['id'],document=r['document'],tag=r['tag'],
                                 received_at=r['dt'],first_read=first,last_read=last,
                                 after_first_read=bool(first and r['dt']>first),
                                 after_all_reads=bool(last and r['dt']>last),
                                 warehouse_dt=row['warehouse_dt'],warehouse_ids=row['warehouse_ids'],
                                 after_warehouse=r['dt']>row['warehouse_dt'],
                                 raw_ids='; '.join(p['first_id']+'…'+p['last_id'] for p in same)))
        details.append(row)
    report['details']=details
    report['processing_coverage']=dict(received=received,recognized=recognized)
    report['accountability']=dict(counts=dict(counts),received=received,recognized=recognized,
                                  late_first_reads=late_first,
                                  unresolved_no_input=counts['NO_INPUT'],
                                  no_instruction=counts['NO_INSTRUCTION'],
                                  after_read=counts['AFTER_READ'],after_warehouse=counts['AFTER_WAREHOUSE'],
                                  processing_review=counts['PROCESSING_REVIEW'],
                                  late_transfers=sum(r['control']['late_initial_passage'] or r['control']['request_after_warehouse'] for r in details),
                                  request_time_source='RfidTags.Dt — момент передачи 1С (подтверждено владельцем обмена)',
                                  instruction_validity='До исполнения, без ограничения возраста',
                                  fulfilment_checked=False)
    report['request_evidence']=sorted(requests,key=lambda r:(r['received_at'],r['series'],r['id']))
    start=datetime.fromisoformat(report['date_from']);end=datetime.fromisoformat(report['date_to'])+timedelta(days=1)
    report['software_days']=[]
    at=start
    while at<end:
        report['software_days'].append(dict(date=at.date().isoformat(),**days[at.date().isoformat()]))
        at+=timedelta(days=1)
    return report


def control_rows(report):
    a=report.get('accountability',{})
    return [
        ['Нет входящего чтения, причина не установлена',a.get('unresolved_no_input','Не проверено')],
        ['Нет распорядки 1С в доступном реестре',a.get('no_instruction','Не проверено')],
        ['Распорядка поступила после доступных чтений',a.get('after_read','Не проверено')],
        ['Первая распорядка поступила после складской записи',a.get('after_warehouse','Не проверено')],
        ['Чтение и заявка есть, требуется разбор распознавания',a.get('processing_review','Не проверено')],
    ]
