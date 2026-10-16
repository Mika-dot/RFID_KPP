import json
import unittest
from deploy.ha.diagnose_cluster import diagnose


class ClusterDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [{"id":name, "url":"http://"+name} for name in ("physical","perimetr","comparator")]
        self.rows = {n["id"]:{"node":n["id"], "sample_age":1, "epoch":816, "fencing_protocol":2,
            "release_sha":"a"*40 if n["id"] == "physical" else "b"*40,
            "active":n["id"] == "physical", "healthy":False, "prepared":False,
            "faulted":n["id"] != "physical", "preflight":{"checks":{"sdk_load":False}},
            "secrets":"PASSWORD=hidden", "events":[{"person":"private"}],
            "services":{"RfidReader":{"ok":False,"detail":{"dependencies":{"business_flow":{"status":"unavailable","detail":"PASSWORD=hidden"}},
                "metrics":{"business_flow_latched":True,"password":"hidden"}}}}} for n in self.nodes}

    def request(self, url, *args, **kwargs):
        self.assertNotIn("body", kwargs)
        return 200, self.rows[url.split("/")[2]]

    def test_screenshot_state_is_not_misreported_as_restored(self):
        result = diagnose(self.nodes,"token",self.request)
        self.assertEqual(["physical"], result["reported_active"])
        self.assertIsNone(result["healthy_owner"])
        self.assertTrue(result["mixed_releases"])
        self.assertEqual(["business_flow"],result["nodes"]["physical"]["services"]["RfidReader"]["failed_dependencies"])
        self.assertNotIn("hidden",json.dumps(result))
        self.assertNotIn("private",json.dumps(result))

    def test_stale_owner_is_excluded_and_unreachable_node_does_not_abort_collection(self):
        self.rows["physical"].update(healthy=True,sample_age=20)
        def request(url,*args,**kwargs):
            if "perimetr" in url:raise TimeoutError("sensitive connection string")
            return self.request(url,*args,**kwargs)
        result = diagnose(self.nodes,"token",request)
        self.assertIsNone(result["healthy_owner"])
        self.assertEqual("TimeoutError",result["nodes"]["perimetr"]["error"])
        self.assertIn("comparator",result["nodes"])
        self.assertNotIn("sensitive",json.dumps(result))
