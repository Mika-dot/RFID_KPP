import importlib.util
import json
import os
import tempfile
import unittest
import copy
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).parents[1]


def load(name):
    path=ROOT/"deploy/ha"/(name+".py")
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class InstallationTests(unittest.TestCase):
    def test_gateway_and_observer_verification_require_exact_installed_release(self):
        module=load("verify_resilience_installation")
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/"nodes.json";config.write_text('{"nodes":[]}',encoding="utf-8")
            argv=["--nodes-config",str(config),"--release","a"*40,"--gateway-url","http://127.0.0.1:5051"]
            with patch.dict(os.environ,{"PERIMETER_HA_TOKEN":"private"}),patch.object(module,"verify",return_value={}),patch.object(module,"json_request",return_value=(200,dict(status="ok",release_sha="b"*40))):
                with self.assertRaisesRegex(RuntimeError,"WrongRelease"):module.main(argv)
            snapshot=dict(release_sha="a"*40,stale=False,status="collecting_baseline",unavailable_sources=[])
            with patch.dict(os.environ,{"PERIMETER_HA_TOKEN":"private"}),patch.object(module,"verify",return_value={}),patch.object(module,"json_request",side_effect=[(200,dict(release_sha="a"*40)),(200,snapshot)]),redirect_stdout(io.StringIO()):
                self.assertEqual(0,module.main(argv+["--observer-url","http://127.0.0.1:19153"]))

    def test_local_copy_gate_rejects_missing_stale_incomplete_and_short_retention(self):
        module = load("verify_resilience_installation")
        node = {"id": "physical", "url": "http://physical"}
        status = {"fallback_enabled": True, "metadata_mirror": {
            "enabled": True, "retention_days": 93, "error": None,
            "streams": {key: {"age_sec": 1, "caught_up": True, "records": 0}
                        for key in module.REQUIRED_STREAMS}}}
        calls = []
        def request(url, token, **kwargs):
            calls.append(url)
            if url.endswith("/fallback/stats"):
                return 200, {"retention_days": 93, "pending": 0}
            return 200, {"node": "physical", "configured": True, "stale": False, "caught_up": True}
        proof = module.verify_local_copies(node, status, "secret", request)
        self.assertTrue(proof["verified"])
        self.assertTrue(all(url.endswith(("/fallback/stats", "/events/recent")) for url in calls))
        variants = []
        missing = copy.deepcopy(status); missing["metadata_mirror"]["streams"].pop("rfid"); variants.append(missing)
        stale = copy.deepcopy(status); stale["metadata_mirror"]["streams"]["events"]["age_sec"] = 140; variants.append(stale)
        partial = copy.deepcopy(status); partial["metadata_mirror"]["streams"]["video"]["caught_up"] = False; variants.append(partial)
        short = copy.deepcopy(status); short["metadata_mirror"]["retention_days"] = 90; variants.append(short)
        rolled_back = copy.deepcopy(status); rolled_back["metadata_mirror"]["error"] = "RuntimeError"; variants.append(rolled_back)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(RuntimeError):
                module.verify_local_copies(node, variant, "secret", request)

    def test_config_preserves_existing_spools_and_business_settings(self):
        module=load("prepare_resilience_config")
        with tempfile.TemporaryDirectory() as folder:
            cfg=dict(node_id="physical",root=folder,state_dir=folder,nodes=[dict(id=n,url="http://"+n,priority=i+1) for i,n in enumerate(("physical","perimetr","comparator"))],
                     env=dict(RFID_SPOOL_PATH=str(Path(folder)/"old-rfid.sqlite"),RFID_VIDEO_SPOOL=str(Path(folder)/"old-video.sqlite"),KPP_RECHECK_HOURS="168"))
            prepared=module.prepare(cfg)
            self.assertEqual(cfg["env"],prepared["env"]);self.assertTrue(prepared["replication_enabled"])
            self.assertNotIn("replication_enabled",cfg)
        with self.assertRaises(ValueError):module.prepare(dict(cfg,env={}))

    @unittest.skipIf(os.name == "nt", "Linux systemd deployment paths")
    def test_systemd_units_keep_credentials_out_of_argv_and_enable_restart(self):
        module=load("install_resilience")
        text=module.unit(Path("/opt/perimeter/source"),Path("/opt/perimeter/venv/bin/python"),Path("/etc/perimeter/gateway.json"),Path("/etc/perimeter/private.env"),"gateway")
        self.assertIn("Restart=always",text);self.assertIn("EnvironmentFile=/etc/perimeter/private.env",text)
        self.assertIn("-m gateway.server",text);self.assertNotIn("TOKEN=",text)

    def test_panels_are_idempotent_and_preserve_existing_objects(self):
        module=load("finish_behavior_monitoring")
        original=dict(uid="existing-dashboard",panels=[dict(id=1,title="Existing HA",options=dict(unchanged=True),gridPos=dict(y=0,h=8))])
        updated=module.panels(original)
        self.assertEqual(updated,module.panels(updated));self.assertEqual(original["panels"][0],updated["panels"][0])
        self.assertEqual(4,len(updated["panels"]))
        bad=dict(panels=[dict(id=191530,description="other owner")])
        with self.assertRaises(RuntimeError):module.panels(bad)

    def test_behavior_summary_uses_existing_wallboard_proxy(self):
        module=load("finish_behavior_monitoring")
        calls=[]
        class Monitor:
            def http_json(self, url):
                calls.append(url)
                return 200, [{"status":"collecting_baseline", "stale":False}]
        summary=module.read_behavior_summary(Monitor())
        self.assertEqual("collecting_baseline", summary[0]["status"])
        self.assertEqual([module.BASE+"/summary"], calls)

    def test_read_only_installation_probe_accepts_one_executor_two_passive_reserves(self):
        module=load("verify_resilience_installation")
        cfg=json.loads((ROOT/"deploy/ha/gateway.example.json").read_text())
        release="f"*40;calls=[]
        def request(url,token,**kwargs):
            calls.append(url)
            if url.endswith("/replica/stats"):return 200,dict(pending=0)
            node=next(n for n in cfg["nodes"] if url.startswith(n["url"]))
            active=node["id"]=="physical"
            return 200,dict(node=node["id"],release_sha=release,fencing_protocol=2,replication_enabled=True,sample_age=1,
                faulted=False,operator_maintenance=False,prepared=True,active=active,healthy=active,epoch=9,
                services={str(i):dict(ok=True) for i in range(5)} if active else {},update=dict(pending=False))
        proof=module.verify(cfg["nodes"],release,"private",request)
        self.assertTrue(proof["installation_ready"]);self.assertEqual("not_measured",proof["physical_business_acceptance"])
        self.assertTrue(all(url.endswith(("/status","/replica/stats")) for url in calls))

    def test_alert_persistence_resets_on_recovery_and_survives_restart(self):
        from observer.notifications import AlertOutbox
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"alerts.sqlite";a=AlertOutbox(path)
            a.transition("cluster","normal",0)
            a.transition("cluster","critical",1,3);a.transition("cluster","critical",2,3)
            a.transition("cluster","normal",3)
            a=AlertOutbox(path);a.transition("cluster","critical",4,3)
            with a.connect() as db:self.assertEqual(0,db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
            a.transition("cluster","critical",5,3);a=AlertOutbox(path);a.transition("cluster","critical",6,3)
            with a.connect() as db:self.assertEqual(1,db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
