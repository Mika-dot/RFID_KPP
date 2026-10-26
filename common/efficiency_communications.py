"""Bounded read-only evidence from Guardian's local Reader logs.

Positive observations only. A local node's rotated logs are not complete HA
availability history; gaps, missing files and silence never become healthy time.
Only allow-listed Reader messages are exported, never exception trace payloads.
"""
from datetime import datetime, timedelta
import os
from pathlib import Path
import re

from common.efficiency_report import MOSCOW


def read_reader_communications(report, directory=None, *, local_zone=None, max_bytes=64*1024*1024):
    if directory is None:
        configured=os.getenv('KPP_REPORT_RFID_LOG_DIR')
        state=os.getenv('PERIMETER_HA_STATE_DIR')
        directory=Path(configured) if configured else Path(state)/'logs' if state else None
    if directory is None:
        return dict(checked=False,complete=False,reason='Журнал Reader этого узла недоступен',events=[],intervals=[])
    try:
        paths=[Path(directory)/('RfidReader.log'+suffix) for suffix in ('.3','.2','.1','')]
        paths=[p for p in paths if p.is_file()]
    except OSError:
        return dict(checked=False,complete=False,reason='Доступ к журналу Reader не подтверждён',events=[],intervals=[])
    if not paths:
        return dict(checked=False,complete=False,reason='Файлы журнала Reader не найдены',events=[],intervals=[])
    zone=local_zone or datetime.now().astimezone().tzinfo
    first,last=None,None
    events=[];size=0;lines=0
    stamp=re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+\[(INFO|WARNING|ERROR|CRITICAL)\]\s+(.*)$')
    try:
        if sum(p.stat().st_size for p in paths)>max_bytes:
            raise ValueError('Лимит журнала')
        for path in paths:
            with path.open(encoding='utf-8',errors='replace') as source:
                for number,line in enumerate(source,1):
                    size+=len(line.encode('utf-8'));lines+=1
                    if size>max_bytes or lines>500000:
                        raise ValueError('Лимит журнала')
                    m=stamp.match(line)
                    if not m:continue
                    at=datetime.fromisoformat(m[1]).replace(tzinfo=zone).astimezone(MOSCOW).replace(tzinfo=None)
                    first=at if first is None else min(first,at);last=at if last is None else max(last,at)
                    text=m[3].strip()
                    code=re.fullmatch(r'TCPConnect code=(-?\d+)',text)
                    if code and int(code[1])!=0:
                        kind='CONNECT_FAILED';detail='TCPConnect code='+code[1]
                    elif re.fullmatch(r'RFID reader connected, epoch=[0-9a-fA-F-]{36}',text):
                        kind='CONNECTED';detail='Соединение Reader установлено'
                    elif text=='RFID connection epoch failed':
                        kind='READER_EXCEPTION';detail='Исключение Reader; причина сеть / SDK / код требует разбора'
                    else:continue
                    events.append(dict(at=at.isoformat(),kind=kind,detail=detail,source=path.name+':'+str(number)))
                    if len(events)>5000:raise ValueError('Лимит событий журнала')
    except (OSError,ValueError):
        return dict(checked=False,complete=False,reason='Проверка журнала не завершена (доступ / лимит)',events=[],intervals=[])
    events.sort(key=lambda e:(e['at'],e['source']))
    intervals=[];pending=None;previous=None
    for e in events:
        at=datetime.fromisoformat(e['at'])
        if pending and (at-previous).total_seconds()>30:
            pending=None
        if e['kind']=='CONNECT_FAILED':
            pending=pending or e;previous=at
        elif e['kind']=='CONNECTED':
            if pending:
                intervals.append(dict(start=pending['at'],end=e['at'],start_source=pending['source'],end_source=e['source'],
                                      kind='LOCAL_CONNECT_FAILURE',duration_sec=int((at-datetime.fromisoformat(pending['at'])).total_seconds())))
            pending=None;previous=at
        else:
            # An exception is not proof that a TCP connection was unavailable.
            pending=None;previous=at
    start=datetime.fromisoformat(report['date_from']);end=datetime.fromisoformat(report['date_to'])+timedelta(days=1)
    return dict(checked=True,complete=False,node=os.getenv('PERIMETER_HA_NODE','локальный узел'),
                reason='Локальные журналы; глобальная доступность всех HA-узлов не измерена',
                first=first.isoformat() if first else None,last=last.isoformat() if last else None,
                events=[e for e in events if start<=datetime.fromisoformat(e['at'])<end],
                intervals=[i for i in intervals if datetime.fromisoformat(i['start'])<end and datetime.fromisoformat(i['end'])>start])
