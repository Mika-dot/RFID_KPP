"""Add managed behavior panels to the EXISTING director dashboard, never a duplicate.

--plan works offline. --apply runs on ub22 after the new observer and wallboard
adapter are installed. Existing datasource, HA panels, recipients and Zabbix
objects are preserved. Credentials are read locally by existing helper only.
"""
from __future__ import annotations
import argparse
import copy
import json
import os
import sys
import uuid
from pathlib import Path

MARKER="Managed Perimeter Behavior Observer v1"
BASE="http://127.0.0.1:19150/perimeter-behavior"
DS={"type":"yesoreyeram-infinity-datasource","uid":"wallboard-api"}


def panels(dashboard):
    d=copy.deepcopy(dashboard);old=d.get("panels",[]);ids={191530,191531,191532}
    if any(p.get("id") in ids and p.get("description")!=MARKER for p in old):
        raise RuntimeError("BehaviorPanelIdCollision")
    remaining=[p for p in old if p.get("id") not in ids]
    y=max((p.get("gridPos",{}).get("y",0)+p.get("gridPos",{}).get("h",0) for p in remaining),default=0)
    def target(route,columns):
        return dict(refId="A",datasource=DS,type="json",source="url",parser="backend",format="table",root_selector="",
            url=BASE+route,url_options=dict(method="GET"),columns=[dict(selector=a,text=b,type=c) for a,b,c in columns])
    new=[dict(id=191530,type="stat",title="Периметр — поведение источников",description=MARKER,datasource=DS,
        gridPos=dict(x=0,y=y,w=24,h=4),options=dict(reduceOptions=dict(calcs=["lastNotNull"],fields="",values=False)),
        targets=[target("/summary",[("status","Статус","string"),("stale","Устаревшие данные","string")])]),
        dict(id=191531,type="table",title="Периметр — отклонения и дрейф",description=MARKER,datasource=DS,
        gridPos=dict(x=0,y=y+4,w=24,h=9),targets=[target("/metrics",[("metric","Метрика","string"),("value","Сейчас","number"),("baseline","База","number"),
            ("anomaly_score","Аномалия","number"),("drift_score","Дрейф","number"),("divergence_score","Расхождение","number"),("status","Статус","string")])]),
        dict(id=191532,type="table",title="Периметр — тенденции и прогноз",description=MARKER,datasource=DS,
        gridPos=dict(x=0,y=y+13,w=24,h=9),targets=[target("/metrics",[("metric","Метрика","string"),("degradation_velocity_per_hour","Изменение/час","number"),
            ("forecast_15min","15 минут","number"),("forecast_1hour","1 час","number"),("forecast_shift","8 часов","number"),("baseline_ready","База готова","string")])])]
    d["panels"]=remaining+new;return d


def apply():
    if sys.platform!="linux" or os.geteuid()!=0:raise RuntimeError("RunOnUb22WithSudo")
    import install_monitoring as monitor
    token,_=monitor.read_env();api="http://127.0.0.1:3000"
    code,response=monitor.http_json(api+"/api/dashboards/uid/mositlab-director-wallboard",token=token)
    if code!=200 or not response.get("meta",{}).get("canSave"):raise RuntimeError("ExistingDashboardNotWritable")
    original=response["dashboard"];updated=panels(original)
    # Require real observer data before publishing panels.
    _,summary=monitor.http_json(BASE+"/summary")
    if not summary or summary[0].get("stale") or summary[0].get("status")=="collector_error":raise RuntimeError("BehaviorBackendUnavailable")
    folder=Path("/var/lib/perimeter-ha-monitor/backups")/("behavior-"+uuid.uuid4().hex);folder.mkdir(parents=True,mode=0o700)
    file=folder/"dashboard.json";file.write_text(json.dumps(original),encoding="utf-8");file.chmod(0o600)
    body=dict(dashboard=updated,overwrite=True,folderUid=response["meta"].get("folderUid"),message="Managed Perimeter behavior panels")
    code,_=monitor.http_json(api+"/api/dashboards/db",token=token,body=body)
    if code!=200:raise RuntimeError("BehaviorDashboardSaveFailed")
    return {"managed_panels":3,"dashboard":"mositlab-director-wallboard"}


def main(argv=None):
    p=argparse.ArgumentParser();m=p.add_mutually_exclusive_group(required=True);m.add_argument("--plan",action="store_true");m.add_argument("--apply",action="store_true")
    a=p.parse_args(argv)
    print(json.dumps({"mode":"plan","panels":3,"changes_applied":False} if a.plan else apply()))


if __name__=="__main__":
    try:main()
    except Exception as exc:print("BEHAVIOR_PANELS_FAILED",type(exc).__name__,file=sys.stderr);raise SystemExit(2)
