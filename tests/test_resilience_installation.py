import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).parents[1]


def load(name):
    path=ROOT/"deploy/ha"/(name+".py")
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class InstallationTests(unittest.TestCase):
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
