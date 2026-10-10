"""Read-only Web routing from two fresh authenticated SQL-lease witnesses.

This does not elect an owner or renew a lease. Missing/stale/disagreeing witnesses
disable live routing; the existing authenticated metadata fallback remains.
"""
from __future__ import annotations
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import math
import time
from guardian.net import json_request
from common.replicated_ingest import validate_peers


class ObservedLease:
    def __init__(self,nodes,token,request=json_request):
        validate_peers(nodes)
        self.nodes,self.token,self.request=nodes,token,request

    def evidence(self,node):
        try:
            started = time.monotonic()
            code,row=self.request(node["url"].rstrip("/")+"/status",self.token,timeout=2)
            elapsed = max(0,time.monotonic()-started)
            lease=row.get("observed_lease",{})
            ids={item["id"] for item in self.nodes}
            if (code!=200 or row.get("node")!=node["id"] or type(row.get("fencing_protocol")) is not int
                    or row["fencing_protocol"]!=2 or type(row.get("sample_age")) not in (int,float)
                    or not 0<=row["sample_age"]<=10-elapsed or type(lease.get("age_sec")) not in (int,float)
                    or not 0<=lease["age_sec"]<=8-elapsed or type(lease.get("epoch")) is not int
                    or lease["epoch"]<0 or lease.get("owner") not in ids|{None}
                    or type(lease.get("valid")) is not bool or type(lease.get("enabled")) is not bool
                    or type(lease.get("remaining_sec")) not in (int,float)
                    or not math.isfinite(lease["remaining_sec"]) or lease["remaining_sec"] < 0):
                return None
            valid = lease["valid"] and lease["remaining_sec"] > lease["age_sec"] + elapsed
            deadline = started + lease["remaining_sec"] - lease["age_sec"]
            fresh_until = started + min(10-row["sample_age"],8-lease["age_sec"])
            return lease["owner"],lease["epoch"],valid,lease["enabled"],deadline,fresh_until
        except Exception:
            return None

    def witness(self,node):
        evidence = self.evidence(node)
        return evidence[:4] if evidence is not None else None

    def lease(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            evidence=list(pool.map(self.evidence,self.nodes))
        now=time.monotonic()
        # Fast witnesses can expire while another request waits for its timeout.
        counts=Counter((v[0],v[1],v[2] and v[4]>now,v[3]) for v in evidence if v is not None and v[5]>now)
        agreed=[value for value,count in counts.items() if count>=2]
        if len(agreed)!=1:
            raise RuntimeError("FreshSqlLeaseWitnessQuorumUnavailable")
        owner,epoch,valid,enabled=agreed[0]
        return {"owner":owner,"epoch":epoch,"valid":valid,"enabled":enabled,"source":"authenticated_guardian_sql_witnesses"}
