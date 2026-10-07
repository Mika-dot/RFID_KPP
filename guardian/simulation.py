"""Exercise the production controller against a shared, transactional lease model.

This validates software invariants; it does not emulate the vendor TCP firmware,
SQL Server's lock manager, physical power or a real LLM.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, patch


def simulate(root):
    sys.path.insert(0,str(root))
    from guardian.controller import Controller
    from guardian.repair import validate_step
    nodes=[dict(id=n,priority=i+1,url="http://"+n) for i,n in enumerate(("physical","perimetr","comparator"))]
    class Model:
        def __init__(self):
            self.value=dict(owner="physical",epoch=10,valid=True,enabled=True,age=200)
            self.faults=set();self.online=True;self.grants=[]
        def lease(self):
            if not self.online:raise TimeoutError("ModeledSqlUnavailable")
            return dict(self.value)
        def node_state(self,n):return dict(faulted=n in self.faults)
        def fault(self,n):self.faults.add(n)
        def claim_controller(self):return self.online
        def grant(self,n):
            if not self.online:raise TimeoutError("ModeledSqlUnavailable")
            if n in self.faults:raise RuntimeError("ModelCandidateInRepair")
            if n!=self.value["owner"] or not self.value["valid"]:self.value["epoch"]+=1
            self.value.update(owner=n,valid=bool(n),age=0);self.grants.append(n)
        def write(self,n,epoch):
            return self.online and self.value["valid"] and n==self.value["owner"] and epoch==self.value["epoch"]
    cfg=dict(node_id="comparator",nodes=nodes,failback_stable_sec=120,startup_sec=120)
    store=Model();telemetry=Mock();controller=Controller(cfg,store,telemetry,threading.Event())
    healthy=lambda n:dict(prepared=True,active=n==store.value["owner"],healthy=n==store.value["owner"],epoch=store.value["epoch"])
    current={n["id"]:healthy(n["id"]) for n in nodes}
    controller.poll=lambda node:(node["id"],current.get(node["id"],{}))
    scenarios=[]
    with patch.dict(os.environ,PERIMETER_HA_TOKEN="offline-only"),patch("guardian.controller.get_json",return_value=(200,{})):
        current["physical"]={};controller.tick()
        if store.value["owner"]!="perimetr":raise RuntimeError("ModelFirstReserveFailed")
        scenarios.append("executor_loss_to_first_reserve")
        if store.write("physical",10):raise RuntimeError("ModelStaleEpochWritable")
        scenarios.append("old_owner_epoch_is_fenced")
        current["perimetr"]={};current["comparator"]=dict(prepared=True)
        controller.tick()
        if store.value["owner"]!="comparator":raise RuntimeError("ModelSecondReserveFailed")
        scenarios.append("first_reserve_loss_to_second_reserve")
        current["comparator"]={};controller.tick()
        if store.value["owner"] is not None:raise RuntimeError("ModelNoReserveStillOwns")
        scenarios.append("all_nodes_unavailable_has_no_owner")
        store.online=False
        try:controller.tick()
        except TimeoutError:pass
        if any(store.write(n["id"],store.value["epoch"]) for n in nodes):raise RuntimeError("ModelSqlLossAllowsWrite")
        scenarios.append("sql_unavailable_fails_closed")
        store.online=True;store.faults.clear()
        current={n["id"]:dict(prepared=True) for n in nodes};controller.tick()
        if store.value["owner"]!="physical":raise RuntimeError("ModelRecoveryFailed")
        scenarios.append("sql_and_nodes_recover")
        for action in ("promote","exec","sql","recovered"):
            try:validate_step(dict(action=action,service="all",reason="modeled malformed LLM response"))
            except ValueError:continue
            raise RuntimeError("ModelUnsafeLlmActionAccepted")
        scenarios.append("invalid_llm_never_selects_owner_or_executes_sql")
        if store.grants.count("physical")<1:raise RuntimeError("ModelNoOwnerWithoutLlm")
        scenarios.append("controller_operates_without_lm_studio")
    return {"status":"passed","scenarios":scenarios,"scenario_count":len(scenarios),"evidence":"software_model_only"}


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument("--root",required=True,type=Path);p.add_argument("--output",type=Path)
    a=p.parse_args(argv);result=simulate(a.root.resolve())
    if a.output:a.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print("HA_SIMULATION_PASSED",result["scenario_count"])


if __name__=="__main__":main()
