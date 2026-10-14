"""Offline regression checks for read-only factory acceptance."""
import json
import unittest

from deploy.ha.verify_factory_acceptance import check_cluster, collect, compare_snapshots

SHA = "7fc779e406c250b69b5fa34c6cab0e0d1d3bd5a6"
NODES = ("physical", "perimetr", "comparator")


class FactoryAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"nodes": [{"id": name, "url": "http://" + name + ":18200"}
                              for name in NODES]}
        self.env = {"PERIMETER_HA_TOKEN":"t"*64, "PERIMETER_HA_SQL":"private"}
        self.responses = {}
        for name in NODES:
            self.responses[name] = {
                "node":name, "sample_age":1, "fencing_protocol":2, "epoch":5340,
                "release_sha":SHA, "active":name=="comparator",
                "healthy":name=="comparator", "faulted":name!="comparator",
                "prepared":True, "operator_maintenance":False,
                "replication_enabled":True,"fallback_enabled":True,
                "update":{"operator_pinned":True,"pending":True,"qualification":"passed"},
                "preflight":{"checks":{}},
                "services": {s:{"ok":True} for s in
                    ("RfidReader","RusGuardSync","Yolo","Aggregator","WebDashboard")}
                    if name=="comparator" else {}
            }
        self.sql={"owner":"comparator","epoch":5340,"lease_valid":True,
                  "controller":"comparator","controller_valid":True,
                  "rfid_max_id":40,"aggregator_cursor":40,"event_max_id":9}
        self.web={"health_http_ok":True,"events_http_ok":True,
                  "events_total":9,"health_status":"OK"}

    def request(self,url,*args,**kwargs):
        self.assertNotIn("body",kwargs)
        name=url.split("/")[2].split(":")[0]
        return 200,self.responses[name]

    def collect(self):
        return collect(self.cfg,self.env,SHA,self.request,
                       lambda _:dict(self.sql),lambda _url,_env:dict(self.web))

    def test_pinned_cluster_accepts_passive_faults_but_never_dual_owner(self):
        result=self.collect()
        self.assertTrue(result["endpoints_ready"])
        self.assertTrue(result["deployment"]["passed"])
        self.assertEqual(["perimetr","physical"],sorted(result["deployment"]["faulted_passive"]))
        self.assertTrue(result["cluster"]["all_nodes_pinned"])
        self.responses["physical"].update(active=True,healthy=True,faulted=False)
        result=self.collect()
        self.assertFalse(result["endpoints_ready"])
        self.assertIn("unique_healthy_owner_missing",result["deployment"]["reasons"])

    def test_wrong_version_or_missing_pin_refused_without_traffic_change(self):
        self.responses["perimetr"]["release_sha"]="a"*40
        result=self.collect()
        self.assertIn("release_mismatch",result["deployment"]["reasons"])
        self.responses["perimetr"]["release_sha"]=SHA
        self.responses["physical"]["update"]["operator_pinned"]=False
        result=self.collect()
        self.assertIn("operator_pin_not_verified",result["deployment"]["reasons"])

    def test_sql_lease_mismatch_prevents_acceptance(self):
        self.sql["owner"]="physical"
        result=self.collect()
        self.assertFalse(result["endpoints_ready"])
        self.assertIn("sql_lease_or_controller_inconsistent",result["checks"])

    def test_readiness_does_not_falsely_claim_real_events(self):
        before={"sql":dict(self.sql),"web":dict(self.web)}
        after=self.collect()
        result=compare_snapshots(before,after)
        self.assertFalse(result["rfid_sql_aggregator_observed"])
        self.assertFalse(result["event_web_progress_observed"])
        self.assertEqual("not_measured",result["full_identity_direction_video_warehouse_proof"])

    def test_real_sql_and_web_progress_are_separate_proofs(self):
        before={"sql":dict(self.sql),"web":dict(self.web)}
        self.sql.update(rfid_max_id=45,aggregator_cursor=45,event_max_id=10)
        self.web["events_total"]=10
        result=compare_snapshots(before,self.collect())
        self.assertEqual(5,result["rfid_new_rows"])
        self.assertTrue(result["rfid_sql_aggregator_observed"])
        self.assertTrue(result["event_web_progress_observed"])
        self.assertEqual("not_measured",result["full_identity_direction_video_warehouse_proof"])

    def test_new_reads_not_caught_up_not_accepted(self):
        before={"sql":dict(self.sql),"web":dict(self.web)}
        self.sql.update(rfid_max_id=45,aggregator_cursor=42,event_max_id=10)
        result=compare_snapshots(before,self.collect())
        self.assertFalse(result["rfid_sql_aggregator_observed"])
        self.assertFalse(result["event_web_progress_observed"])

    def test_credentials_and_raw_records_not_emitted(self):
        data=json.dumps(self.collect())
        self.assertNotIn("private",data)
        self.assertNotIn("t"*64,data)
        self.assertNotIn("ClientReadUuid",data)


if __name__ == "__main__":
    unittest.main()
