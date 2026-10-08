import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from guardian.controller import Controller
from guardian.hardware_fence import fence_previous
from guardian.qualification import classify, offline_env, mechanical, qualify

ROOT=Path(__file__).parents[1]


def copy_candidate(root):
    for name in ("guardian","common","KPP","deploy","RFID_reader_v4","RTSP","web","DB_RusGard"):
        for path in (ROOT/name).rglob("*.py"):
            target=root/path.relative_to(ROOT)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,target)
    shutil.copyfile(ROOT/"guardian/runtime_contract.json",root/"guardian/runtime_contract.json")


class QualificationTests(unittest.TestCase):
    def test_classes_cover_schema_dependencies_models_monitoring_and_business(self):
        self.assertEqual(["DOCS_ONLY"],classify(["deploy/ha/README.md"]))
        for path,kind in (("migrations/005.sql","DB_SCHEMA"),("guardian/requirements.txt","DEPENDENCIES"),
                          ("RTSP/model.pt","MODEL_OR_DLL"),("guardian/node.py","HA_GUARDIAN"),
                          ("observer/behavior.py","MONITORING_ONLY"),("KPP/aggregator.py","BUSINESS_LOGIC")):
            self.assertIn(kind,classify([path,"README.md"]))

    def test_offline_child_does_not_receive_production_credentials_or_sql_test_target(self):
        with patch.dict("os.environ",PERIMETER_HA_SQL="secret",RFID_READER_IP="factory",
                        PERIMETER_HA_TOKEN="token",PERIMETER_HA_TEST_SQL="test-database",PYTHONPATH="unsafe"):
            env=offline_env()
        for name in ("PERIMETER_HA_SQL","RFID_READER_IP","PERIMETER_HA_TOKEN","PERIMETER_HA_TEST_SQL","PYTHONPATH"):
            self.assertNotIn(name,env)

    def test_full_installed_pipeline_accepts_current_production_sources(self):
        proof=qualify(ROOT,ROOT)
        self.assertEqual(15,proof["golden_traces"]);self.assertEqual(0,proof["shadow_differences"])
        self.assertEqual(6,proof["adapter_contracts"]);self.assertEqual(8,proof["ha_model_scenarios"])

    def test_candidate_owned_green_suite_cannot_hide_changed_sql_source_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            copy_candidate(root)
            path=root/"RFID_reader_v4/rfid_to_sql_v4.py"
            text=path.read_text(encoding="utf-8").replace("source_dt = datetime.fromisoformat(source_time)","source_dt = datetime.now()")
            path.write_text(text,encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError,"AdapterContractFailed"):
                qualify(root,ROOT)

    def test_changed_connection_destination_is_rejected_before_running_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            copy_candidate(root)
            path=root/"RFID_reader_v4/rfid_to_sql_v4.py"
            text=path.read_text(encoding="utf-8")
            self.assertIn('os.getenv("RFID_DB_CONNECTION", "")',text)
            path.write_text(text.replace('os.getenv("RFID_DB_CONNECTION", "")',
                                         'os.getenv("RFID_OTHER_DB_CONNECTION", "")'),encoding="utf-8")
            with patch("guardian.qualification.subprocess.run") as child:
                with self.assertRaisesRegex(RuntimeError,"CandidateBusinessDestinationChanged"):
                    qualify(root,ROOT)
                child.assert_not_called()


class HardwareFenceModelTests(unittest.TestCase):
    def cfg(self):
        return dict(node_id="comparator",hardware_fencing_required=True,nodes=[
            dict(id=n,priority=i+1,url="http://"+n,fence_argv=["configured-adapter"],unfence_argv=["configured-unfence"])
            for i,n in enumerate(("physical","perimetr","comparator"))])

    def test_exit_zero_without_verified_isolation_is_not_a_fence(self):
        with patch("guardian.hardware_fence.subprocess.run",return_value=Mock(returncode=0,stdout=b'{"node":"physical","isolated":false,"epoch":11}')):
            with self.assertRaisesRegex(RuntimeError,"ReceiptInvalid"):fence_previous(self.cfg(),"physical",11)

    def test_verified_fence_uses_fixed_argv_and_no_shell(self):
        with patch("guardian.hardware_fence.subprocess.run",return_value=Mock(returncode=0,stdout=b'{"node":"physical","isolated":true,"epoch":11}')) as run:
            self.assertTrue(fence_previous(self.cfg(),"physical",11)["verified"])
        self.assertEqual(["configured-adapter"],run.call_args.args[0]);self.assertFalse(run.call_args.kwargs["shell"])

    def test_failed_fence_survives_controller_recreation_and_blocks_new_owner(self):
        cfg=self.cfg();lease=dict(owner="physical",valid=True,enabled=True,age=200,epoch=10)
        pending=[];store=Mock();store.lease.side_effect=lambda:dict(lease)
        store.node_state.return_value=dict(faulted=False);store.claim_controller.return_value=True
        store.pending_fences.side_effect=lambda:list(pending)
        def grant(owner,**kwargs):
            if kwargs.get("fence_previous"):pending.append((kwargs["fence_previous"],lease["epoch"]+1))
            if owner!=lease["owner"]:lease["epoch"]+=1
            lease.update(owner=owner,valid=bool(owner),age=0)
        store.grant.side_effect=grant
        snapshots={"physical":{},"perimetr":dict(prepared=True),"comparator":dict(prepared=True)}
        with patch.dict("os.environ",PERIMETER_HA_TOKEN="offline"),patch("guardian.controller.get_json",return_value=(200,{})),\
             patch("guardian.hardware_fence.fence_previous",side_effect=RuntimeError("Modeled fence timeout")) as fence:
            for _ in range(2):
                controller=Controller(cfg,store,Mock(),threading.Event())
                controller.poll=lambda n:(n["id"],snapshots[n["id"]])
                with self.assertRaises(RuntimeError):controller.tick()
                self.assertIsNone(lease["owner"])
            self.assertEqual(2,fence.call_count)
        self.assertEqual([("physical",11)],pending)
        self.assertFalse(any(c.args[0]=="perimetr" for c in store.grant.call_args_list))
