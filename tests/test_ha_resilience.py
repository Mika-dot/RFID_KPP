"""Fault models and real loopback HTTP. No factory/reader/SQL connection."""
from __future__ import annotations
import base64
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from common.replicated_ingest import ReplicaJournal, ReplicatedDelivery, envelope, replay_local
from gateway.server import Router, create_server
from guardian.node import Node
from guardian.probation import BusinessProbation
from guardian.recovery import RecoveryTimings
from observer.behavior import BehaviorObserver, cross_source, hypotheses
from observer.notifications import AlertOutbox
from guardian.net import json_request

TOKEN = "test-token-for-local-only-"+"x"*32
NODES = [dict(id=n,priority=i+1,url="http://"+n+":18200",web_url="http://"+n+":5050")
         for i,n in enumerate(("physical","perimetr","comparator"))]


def rfid_payload():
    return dict(client_uuid=str(uuid.uuid4()),source_time="2026-10-07T08:00:00.123456",
        source_sequence=17,connection_epoch="connection-before-failure",antenna=2,rssi=-51.2,
        epc="A"*24,tid="B"*24,time_quality="SOURCE_TIME")


def start_server(test, server):
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    def stop():
        server.shutdown(); server.server_close(); thread.join(2)
    test.addCleanup(stop)
    return "http://127.0.0.1:"+str(server.server_port)


class ReplicaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.journals={n["id"]:ReplicaJournal(self.root/(n["id"]+".sqlite"),n["id"]) for n in NODES}
        self.calls=[]
        def request(url,body):
            name=urlsplit(url).hostname;self.calls.append((name,url))
            return 200,self.journals[name].put(body)
        self.client=ReplicatedDelivery(self.journals["physical"],NODES,request)

    def test_ack_is_persistent_and_quorum_avoids_unneeded_second_peer_roundtrip(self):
        record=self.client.ensure("rfid",rfid_payload())
        self.assertEqual(2,self.client.journal.ack_count(record))
        self.assertEqual(1,len(self.calls))
        restarted=ReplicaJournal(self.root/"perimetr.sqlite","perimetr")
        self.assertEqual(record,restarted.page()["items"][0]["record"])

    def test_lost_origin_disk_recovers_original_uuid_time_tid_and_sequence(self):
        from RFID_reader_v4.rfid_to_sql_v4 import Spool
        payload=rfid_payload();record=self.client.ensure("rfid",payload)
        recovered=Spool(str(self.root/"recovered.sqlite"))
        self.journals["physical"].path.unlink()
        replay_local(self.journals["perimetr"],{"rfid":recovered.path,"video":self.root/"missing"})
        row=recovered.next_pending()
        self.assertEqual(tuple(payload[k] for k in ("client_uuid","source_time","source_sequence","connection_epoch","antenna","rssi","epc","tid","time_quality")),row[:-1])
        replay_local(self.journals["perimetr"],{"rfid":recovered.path,"video":self.root/"missing"})
        self.assertEqual(1,recovered.stats()[0])

    def test_unavailable_peer_has_cooldown_while_other_peer_keeps_delivery_available(self):
        request=self.client.request
        def fail_one(url,body):
            if urlsplit(url).hostname=="perimetr":
                self.calls.append(("failed",url));raise TimeoutError()
            return request(url,body)
        self.client.request=fail_one
        for _ in range(5):self.client.ensure("rfid",rfid_payload())
        self.assertEqual(1,sum(name=="failed" for name,_ in self.calls))
        self.assertEqual(5,self.journals["comparator"].stats()["pending"])

    def test_no_remote_ack_refuses_sql_eligibility_but_keeps_local_copy(self):
        self.client.request=lambda *a:(_ for _ in ()).throw(TimeoutError())
        with self.assertRaisesRegex(RuntimeError,"QuorumUnavailable"):
            self.client.ensure("rfid",rfid_payload())
        self.assertEqual(1,self.client.journal.stats()["pending"])
        self.assertEqual(0,self.client.journal.stats()["quorum_pending"])

    def test_ack_lost_after_peer_commit_is_safe_to_retry(self):
        real=self.client.request
        def lost(url,body):
            real(url,body);raise TimeoutError()
        self.client.request=lost
        payload=rfid_payload()
        with self.assertRaises(RuntimeError):self.client.ensure("rfid",payload)
        self.client.request=real;self.client.peer_retry.clear()
        self.client.ensure("rfid",payload)
        self.assertEqual(1,self.journals["perimetr"].stats()["records"])

    def test_forged_or_wrong_peer_receipt_does_not_count_as_quorum(self):
        self.client.request=lambda url,body:(200,dict(durable=True,node="other",uuid=body["uuid"],digest=body["digest"]))
        with self.assertRaises(RuntimeError):self.client.ensure("rfid",rfid_payload())

    def test_uuid_cannot_be_reused_with_different_contents(self):
        payload=rfid_payload();self.client.ensure("rfid",payload)
        with self.assertRaisesRegex(ValueError,"ContentConflict"):
            self.client.journal.put(envelope("rfid",dict(payload,rssi=-70)))

    def test_checksum_corruption_is_rejected_before_persistence(self):
        record=envelope("rfid",rfid_payload());record["payload"]["epc"]="C"*24
        with self.assertRaises(ValueError):self.client.journal.put(record)
        self.assertEqual(0,self.client.journal.stats()["records"])

    def test_video_copy_keeps_image_bytes_and_group_count(self):
        payload=dict(event_uuid=str(uuid.uuid4()),direction="0>1",from_camera=0,to_camera=1,
            captured_at="2026-10-07T08:00:00",processed_at="2026-10-07T08:00:02",
            time_diff_sec=1.25,transport="FORKLIFT",reel_count=2,source_track_ids=[11,12])
        record=self.client.ensure("video",payload,b"\xff\xd8original-JPEG\xff\xd9")
        copied=self.journals["perimetr"].page()["items"][0]["record"]
        self.assertEqual(record,copied)
        self.assertEqual(b"\xff\xd8original-JPEG\xff\xd9",base64.b64decode(copied["image"]))

    def test_sent_tombstone_survives_duplicate_delivery(self):
        record=self.client.ensure("rfid",rfid_payload());j=self.client.journal
        j.sent(record["stream"],record["uuid"],record["digest"]);j.put(record)
        self.assertEqual(0,j.stats()["pending"])

    def test_retention_removes_only_old_sent_records(self):
        pending=self.client.ensure("rfid",rfid_payload());sent=self.client.ensure("rfid",rfid_payload())
        self.client.journal.sent("rfid",sent["uuid"],sent["digest"])
        with self.client.journal.connect() as db:db.execute("UPDATE replica SET created=1,sent=1")
        self.client.journal.maintenance()
        self.assertEqual(pending,self.client.journal.page()["items"][0]["record"])

    def test_local_uuid_conflict_does_not_overwrite_existing_pending(self):
        from RFID_reader_v4.rfid_to_sql_v4 import Spool
        payload=rfid_payload();self.client.ensure("rfid",payload)
        local=Spool(str(self.root/"conflict.sqlite"));local.enqueue(dict(payload,epc="C"*24))
        with self.assertRaisesRegex(ValueError,"LocalReplicaUuidConflict"):
            replay_local(self.client.journal,{"rfid":local.path,"video":self.root/"missing"})
        self.assertEqual("C"*24,local.next_pending()[6])

    @patch.dict(os.environ,{"PERIMETER_HA_TOKEN":TOKEN})
    def test_real_node_http_requires_auth_and_commits_before_ack(self):
        node=Node.__new__(Node);node.cfg={"state_dir":str(self.root),"listen":"127.0.0.1","port":0}
        node.replica=self.journals["perimetr"]
        url=start_server(self,node.server());record=envelope("rfid",rfid_payload())
        self.assertEqual(401,json_request(url+"/replica/put",body=record)[0])
        code,receipt=json_request(url+"/replica/put",TOKEN,body=record)
        self.assertEqual(200,code);self.assertTrue(receipt["durable"])
        restarted=ReplicaJournal(self.root/"perimetr.sqlite","perimetr")
        self.assertEqual(record,restarted.page()["items"][0]["record"])
        self.assertEqual(200,json_request(url+"/replica/commit",TOKEN,body={k:record[k] for k in ("stream","uuid","digest")})[0])
        self.assertEqual(0,restarted.stats()["pending"])


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.backends=[]
        for label in ("physical","perimetr","comparator"):
            class Handler(BaseHTTPRequestHandler):
                def do_GET(handler):
                    if handler.headers.get("Authorization")!="Basic test-browser-login":
                        handler.send_response(401);handler.send_header("WWW-Authenticate",'Basic realm="Perimeter"');handler.end_headers();return
                    raw=(handler.server.label+handler.path).encode()
                    handler.send_response(200);handler.send_header("Content-Type","image/jpeg");handler.send_header("Content-Length",str(len(raw)));handler.end_headers();handler.wfile.write(raw)
                def log_message(self,*args):pass
            server=ThreadingHTTPServer(("127.0.0.1",0),Handler);server.label=label
            self.backends.append(start_server(self,server))
        self.nodes=[dict(n,web_url=self.backends[i]) for i,n in enumerate(NODES)]
        self.lease=dict(owner="physical",epoch=1,valid=True,enabled=True)
        self.store=Mock();self.store.lease.side_effect=lambda:dict(self.lease)
        self.status=dict(active=True,healthy=True,epoch=1,sample_age=0,fencing_protocol=2,operator_maintenance=False)
        def request(url,token,**kwargs):
            self.assertEqual(TOKEN,token)
            return 200,dict(self.status,node=urlsplit(url).hostname)
        self.router=Router(self.nodes,self.store,TOKEN,request)
        self.url=start_server(self,create_server(self.router,"127.0.0.1",0))

    def fetch(self,path="/api/image/101",auth=True):
        req=Request(self.url+path,headers={"Authorization":"Basic test-browser-login"} if auth else {})
        try:return urlopen(req,timeout=3)
        except HTTPError as exc:return exc

    def test_same_url_follows_all_three_owners(self):
        for i,owner in enumerate(("physical","perimetr","comparator"),1):
            self.lease.update(owner=owner,epoch=i);self.status["epoch"]=i
            with self.fetch() as response:
                self.assertEqual(200,response.status);self.assertEqual((owner+"/api/image/101").encode(),response.read())

    def test_browser_authentication_is_preserved(self):
        with self.fetch(auth=False) as response:
            self.assertEqual(401,response.status);self.assertIn("Basic",response.headers["WWW-Authenticate"])

    def test_no_owner_returns_503_without_fallback_to_old_host(self):
        self.lease["valid"]=False
        with self.fetch() as response:self.assertEqual(503,response.status)

    def test_stale_wrong_epoch_or_unhealthy_snapshot_returns_503(self):
        for change in (dict(sample_age=11),dict(epoch=0),dict(healthy=False),dict(operator_maintenance=True)):
            with self.subTest(change=change):
                previous=dict(self.status);self.status.update(change)
                with self.fetch() as response:self.assertEqual(503,response.status)
                self.status=previous

    def test_epoch_change_while_response_is_in_flight_discards_response(self):
        original=self.router.unchanged
        def changed(identity):self.lease["epoch"]+=1;return original(identity)
        self.router.unchanged=changed
        with self.fetch() as response:self.assertEqual(503,response.status)

    def test_sql_unavailable_returns_503(self):
        self.store.lease.side_effect=TimeoutError()
        with self.fetch() as response:self.assertEqual(503,response.status)

    def test_backend_url_with_credentials_is_rejected(self):
        bad=[dict(n) for n in self.nodes];bad[0]["web_url"]="http://user:secret@other/"
        with self.assertRaises(ValueError):Router(bad,self.store,TOKEN)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.engine=BehaviorObserver(self.root/"history.sqlite",min_history_days=0,min_samples=10)
        self.at=1791350000
        for i in range(12):self.engine.observe(self.at+i*60,{"cursor_lag":10+(i%3-1)})

    def test_baseline_requires_elapsed_days_even_with_many_samples(self):
        engine=BehaviorObserver(self.root/"real-baseline.sqlite")
        result={}
        for i in range(50):result=engine.observe(self.at+i*60,{"cursor_lag":10})
        self.assertFalse(result["cursor_lag"]["baseline_ready"])

    def test_single_outlier_is_anomaly_but_not_sustained_drift(self):
        r=self.engine.observe(self.at+12*60,{"cursor_lag":100})["cursor_lag"]
        self.assertGreater(r["anomaly_score"],30);self.assertEqual(0,r["drift_score"])

    def test_sustained_drift_is_detected_and_predicts_backlog_growth(self):
        r={}
        for i in range(12,17):r=self.engine.observe(self.at+i*60,{"cursor_lag":100+i*10})["cursor_lag"]
        self.assertEqual("critical",r["status"]);self.assertGreater(r["forecast_1hour"],r["value"])
        self.assertEqual("Aggregator",hypotheses({"cursor_lag":r})[0]["zone"])

    def test_drift_state_survives_restart(self):
        for i in range(12,15):self.engine.observe(self.at+i*60,{"cursor_lag":100})
        restarted=BehaviorObserver(self.root/"history.sqlite",min_history_days=0,min_samples=10)
        r=restarted.observe(self.at+15*60,{"cursor_lag":100})["cursor_lag"]
        self.assertGreaterEqual(r["persistent_samples"],4)

    def test_gap_resets_drift_persistence(self):
        for i in range(12,15):self.engine.observe(self.at+i*60,{"cursor_lag":100})
        r=self.engine.observe(self.at+30*60,{"cursor_lag":100})["cursor_lag"]
        self.assertEqual(1,r["persistent_samples"]);self.assertEqual(0,r["drift_score"])

    def test_missing_sources_and_low_traffic_do_not_create_zero_residual(self):
        self.assertNotIn("video_per_rfid_group",cross_source({"rfid_groups_5min":10}))
        self.assertNotIn("video_per_rfid_group",cross_source({"rfid_groups_5min":1,"video_events_5min":0}))
        self.assertEqual(.5,cross_source({"rfid_groups_5min":10,"video_events_5min":5})["video_per_rfid_group"])

    def test_nonmonotonic_or_nan_sample_is_rejected(self):
        with self.assertRaises(ValueError):self.engine.observe(self.at,{"cursor_lag":100})
        with self.assertRaises(ValueError):self.engine.observe(self.at+12*60,{"cursor_lag":float("nan")})

    def test_alerts_deduplicate_recover_and_retry_durably(self):
        alerts=AlertOutbox(self.root/"alerts.sqlite")
        alerts.transition("HA","critical",self.at);alerts.transition("HA","critical",self.at+1)
        self.assertEqual(0,alerts.deliver())
        requests=[]
        def request(url,token,**kwargs):requests.append(kwargs["body"]);return 200,None
        self.assertEqual(1,alerts.deliver("http://local-webhook",request=request))
        self.assertEqual(0,alerts.deliver("http://local-webhook",request=request))
        alerts.transition("HA","normal",self.at+2)
        self.assertEqual(1,alerts.deliver("http://local-webhook",request=request))
        self.assertEqual("critical",requests[1]["previous"])


class TimingAndProbationTests(unittest.TestCase):
    def test_readiness_rto_requires_matching_epoch_and_target(self):
        tracker=RecoveryTimings()
        for kind,t in (("detected",10),("fenced",11),("granted",13)):
            tracker.record(dict(kind="handoff_"+kind,time=t,target="perimetr",epoch=9))
        tracker.record(dict(kind="handoff_ready",time=20,target="perimetr",epoch=8))
        self.assertIsNone(tracker.last)
        tracker.record(dict(kind="handoff_ready",time=21,target="perimetr",epoch=9))
        self.assertEqual(11,tracker.last["readiness_rto_sec"])

    def test_backlog_without_sql_delivery_fails_candidate(self):
        p=BusinessProbation(120)
        self.assertIsNone(p.assess(dict(rfid_pending=1,rfid_sent=0),0))
        self.assertEqual("candidate_rfid_delivery_stalled",p.assess(dict(rfid_pending=50,rfid_sent=0),121))

    def test_delivery_progress_and_quiet_traffic_do_not_fail_candidate(self):
        p=BusinessProbation(120)
        p.assess(dict(rfid_pending=1,rfid_sent=0),0)
        self.assertIsNone(p.assess(dict(rfid_pending=50,rfid_sent=10),121))
        self.assertIsNone(p.assess(dict(rfid_pending=0,rfid_sent=10),300))
