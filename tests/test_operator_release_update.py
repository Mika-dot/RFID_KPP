import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deploy.ha import release_update as update
from guardian.release_contract import MANIFEST


class OperatorUpdateTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.release="b"*40;self.old="a"*40
        self.nodes=[dict(id=name,priority=i+1,url="http://"+name+":18200")
                    for i,name in enumerate(("physical","perimetr","comparator"))]
        self.cfg=dict(node_id="comparator",nodes=self.nodes,root=str(self.root/"source"),update_source=str(self.root/"source"),
            state_dir=str(self.root/"state"),release_dir=str(self.root/"releases"),python="python",env=dict(
            RFID_SPOOL_PATH=str(self.root/"persistent/rfid.sqlite"),RFID_VIDEO_SPOOL=str(self.root/"persistent/video.sqlite")))
        self.state=Path(self.cfg["state_dir"]);self.state.mkdir()
        self.args=SimpleNamespace(release=self.release,config=self.root/"node.json",environment=self.root/"private.env",handoff=False)
        update.write_private(self.args.config,self.cfg);self.args.environment.write_text("private",encoding="utf-8")
        self.record=dict(current=dict(sha=self.old,root=self.cfg["root"],python="python"),pending=False)
        update.write_private(self.state/"release.json",self.record)
        self.folder=self.state/"prepared"/self.release;self.folder.mkdir(parents=True)
        self.candidate=Path(self.cfg["release_dir"])/self.release;self.candidate.mkdir(parents=True)
        update.write_private(self.folder/"node.json",update.candidate_config(self.cfg,{}))
        self.receipt=dict(release=self.release,qualified=True,base=self.record["current"],root=str(self.candidate),tree="tree",
            config_digest=update.digest(self.args.config),record_digest=update.digest(self.state/"release.json"),
            candidate_config_digest=update.digest(self.folder/"node.json"),environment_digest=update.digest(self.args.environment))
        update.write_private(self.folder/"receipt.json",self.receipt)
        self.rows={n["id"]:dict(node=n["id"],sample_age=1,fencing_protocol=2,epoch=9,active=n["id"]=="physical",
            healthy=n["id"]=="physical",release_sha=self.old,prepared=True,faulted=False,operator_maintenance=False) for n in self.nodes}
        self.env={"PERIMETER_HA_TOKEN":"x"*64}

    def test_preparation_changes_no_services_and_runs_tests_from_candidate(self):
        manifest=self.candidate/MANIFEST;manifest.parent.mkdir(parents=True,exist_ok=True)
        manifest.write_bytes((update.ROOT/MANIFEST).read_bytes())
        def git(cfg,*args):
            if "get-url" in args:return "https://github.com/Mika-dot/RFID_KPP.git"
            if "HEAD" in args:return self.release
            if "HEAD^{tree}" in args:return "tree"
            return ""
        with patch.object(update,"git",side_effect=git),patch.object(update,"run") as run,patch.object(update,"native") as native:
            result=update.prepare(self.args,self.cfg,self.env)
        native.assert_not_called()
        self.assertFalse(result["services_changed"])
        self.assertEqual(self.record,json.loads((self.state/"release.json").read_text(encoding="utf-8")))
        self.assertEqual(self.candidate,run.call_args_list[-1].kwargs["cwd"])
        value=json.loads((self.folder/"node.json").read_text(encoding="utf-8"))
        self.assertEqual(self.cfg["env"]["RFID_SPOOL_PATH"],value["env"]["RFID_SPOOL_PATH"])
        self.assertFalse(value["auto_update"]);self.assertEqual("shadow",value["env"]["KPP_ADAPTIVE_WINDOWS_MODE"])

    def test_changed_secret_config_record_or_candidate_refuses_before_native_stop(self):
        with patch.object(update,"git",return_value="tree"):
            self.args.environment.write_text("changed",encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError,"InputsChanged"):
                update.check_prepared(self.args,self.cfg)
            self.args.environment.write_text("private",encoding="utf-8")
            update.write_private(self.folder/"node.json",{"changed":True})
            with self.assertRaisesRegex(RuntimeError,"InputsChanged"):
                update.check_prepared(self.args,self.cfg)

    def test_relative_spools_are_frozen_at_actual_installed_worker_root(self):
        cfg=copy.deepcopy(self.cfg);cfg["env"]["RFID_SPOOL_PATH"]="existing.sqlite"
        cfg["env"]["RFID_READER_LOCK_FILE"]="existing.lock"
        runtime=self.root/"actual-runtime"
        prepared=update.candidate_config(cfg,{},runtime)
        self.assertEqual((runtime/"existing.sqlite").resolve(),Path(prepared["env"]["RFID_SPOOL_PATH"]).resolve())
        self.assertEqual((runtime/"existing.lock").resolve(),Path(prepared["env"]["RFID_READER_LOCK_FILE"]).resolve())
        self.assertEqual("existing.sqlite",cfg["env"]["RFID_SPOOL_PATH"])

    def test_active_upgrade_without_handoff_is_rejected_without_service_changes(self):
        self.cfg["node_id"]="physical"
        for key in ("perimetr","comparator"):self.rows[key]["release_sha"]=self.release
        with patch.object(update,"check_prepared",return_value=(self.folder,self.receipt)),patch.object(update,"inventory",return_value=(self.rows,"physical")),patch.object(update,"native") as native:
            with self.assertRaisesRegex(RuntimeError,"HandoffMode"):
                update.apply(self.args,self.cfg,self.env)
        native.assert_not_called();self.assertFalse((self.state/"operator-release-install.json").exists())

    def test_handoff_cannot_ignore_another_active_owner_or_unknown_epoch(self):
        self.rows["perimetr"].update(active=True,healthy=True,release_sha=self.release,update=dict(pending=False))
        self.rows["physical"]["update"]={"pending":False}
        def request(url,*args,**kwargs):return 200,next(row for row in self.rows.values() if url.startswith("http://"+row["node"]+":"))
        with self.assertRaisesRegex(RuntimeError,"HandoffNotConfirmed"):
            update.wait_handoff(self.cfg,"token","comparator",self.release,request,timeout=0)
        self.rows["physical"].update(active=False,epoch=10)
        with self.assertRaises(RuntimeError):update.wait_handoff(self.cfg,"token","comparator",self.release,request,timeout=0)
        self.rows["physical"]["epoch"]=9
        self.assertEqual("perimetr",update.wait_handoff(self.cfg,"token","comparator",self.release,request,timeout=0)["node"])

    def test_degraded_inventory_is_opt_in_and_rejects_two_owners(self):
        self.rows["physical"]["healthy"] = False
        def request(url,*args,**kwargs):return 200,next(row for row in self.rows.values() if url.startswith("http://"+row["node"]+":"))
        with self.assertRaisesRegex(RuntimeError,"HealthySingleOwnerRequired"):
            update.inventory(self.cfg,"token",request)
        self.assertEqual("physical",update.inventory(self.cfg,"token",request,allow_degraded=True)[1])
        self.rows["physical"]["active"] = False
        self.assertIsNone(update.inventory(self.cfg,"token",request,allow_degraded=True)[1])
        self.rows["physical"]["active"] = self.rows["perimetr"]["active"] = True
        with self.assertRaises(RuntimeError):update.inventory(self.cfg,"token",request,allow_degraded=True)

    def test_legacy_shadow_exceptions_are_bound_to_clean_exact_baseline(self):
        sha, tree = next(iter(update.LEGACY_BASELINES.items()))
        self.record["current"]["sha"] = sha
        def git(cfg,*args):
            if "HEAD" in args:return sha
            if "HEAD^{tree}" in args:return tree
            return ""
        with patch.object(update,"git",side_effect=git):
            self.assertEqual("--allowlist",update.transition_allowlist(self.cfg,self.record,Path(self.cfg["root"]))[0])
        with patch.object(update,"git",return_value="dirty"):
            with self.assertRaisesRegex(RuntimeError,"BaselineChanged"):
                update.transition_allowlist(self.cfg,self.record,Path(self.cfg["root"]))
        self.record["current"]["sha"] = self.old
        with patch.object(update,"git") as git:
            self.assertEqual([],update.transition_allowlist(self.cfg,self.record,Path(self.cfg["root"])))
            git.assert_not_called()

    def test_recovery_flag_never_updates_physical_or_active_reserve(self):
        self.args.recover_reserve = True
        for own, owner in (("physical","physical"),("physical",None),("comparator","comparator")):
            self.cfg["node_id"] = own
            with patch.object(update,"check_prepared",return_value=(self.folder,self.receipt)),patch.object(update,"inventory",return_value=(self.rows,owner)),patch.object(update,"native") as native:
                with self.assertRaisesRegex(RuntimeError,"PassiveLinuxReserve"):
                    update.apply(self.args,self.cfg,self.env)
                native.assert_not_called()

    def test_successful_passive_install_preserves_bootstrap_and_spools(self):
        requests=[]
        def request(url,token,**kwargs):
            requests.append((url,kwargs))
            if url.endswith("/maintenance"):return 200,{}
            maintenance=sum(u.endswith("/maintenance") for u,_ in requests)<2
            return 200,dict(node="comparator",sample_age=1,fencing_protocol=2,release_sha=self.release,
                operator_maintenance=maintenance,active=False,preflight=dict(ok=True),prepared=True,faulted=False,
                replication_enabled=True,fallback_enabled=True)
        with patch.object(update,"check_prepared",return_value=(self.folder,self.receipt)),patch.object(update,"inventory",return_value=(self.rows,"physical")),patch.object(update,"native") as native,patch.object(update,"json_request",side_effect=request),patch("guardian.processes.Processes") as processes:
            result=update.apply(self.args,self.cfg,self.env)
        self.assertTrue(result["installed"]);self.assertFalse((self.state/"operator-release-install.json").exists())
        installed=json.loads(self.args.config.read_text(encoding="utf-8"))
        record=json.loads((self.state/"release.json").read_text(encoding="utf-8"))
        self.assertEqual(self.cfg["root"],installed["root"])
        self.assertEqual(self.cfg["env"]["RFID_SPOOL_PATH"],installed["env"]["RFID_SPOOL_PATH"])
        self.assertEqual(str(self.candidate),record["current"]["root"])
        self.assertTrue(record["pending"])  # Passive readiness is not completed business probation.
        processes.return_value.reap_orphans.assert_called_once()
        self.assertEqual([("stop",),("start",)],[call.args for call in native.call_args_list])

    def test_failure_restores_config_record_and_maintenance(self):
        requests=Mock(side_effect=[(200,{}),RuntimeError("offline")])
        with patch.object(update,"check_prepared",return_value=(self.folder,self.receipt)),patch.object(update,"inventory",return_value=(self.rows,"physical")),patch.object(update,"native") as native,patch.object(update,"json_request",requests),patch.object(update.time,"monotonic",side_effect=[0,200]),patch("guardian.processes.Processes"):
            with self.assertRaisesRegex(RuntimeError,"PreflightFailed"):
                update.apply(self.args,self.cfg,self.env)
        self.assertEqual(self.cfg,json.loads(self.args.config.read_text(encoding="utf-8")))
        self.assertEqual(self.record,json.loads((self.state/"release.json").read_text(encoding="utf-8")))
        self.assertFalse((self.state/"operator-maintenance.json").read_text(encoding="utf-8").find('"enabled": false')<0)
        self.assertEqual([("stop",),("start",),("stop",),("start",)],[call.args for call in native.call_args_list])

    def test_ambiguous_maintenance_response_is_restored_even_before_stop(self):
        request=Mock(side_effect=[TimeoutError(),(200,{})])
        with patch.object(update,"check_prepared",return_value=(self.folder,self.receipt)),patch.object(update,"inventory",return_value=(self.rows,"physical")),patch.object(update,"native") as native,patch.object(update,"json_request",request):
            with self.assertRaises(TimeoutError):update.apply(self.args,self.cfg,self.env)
        native.assert_not_called();self.assertFalse((self.state/"operator-release-install.json").exists())
        self.assertFalse(request.call_args.kwargs["body"]["enabled"])

    def test_failed_rollback_keeps_journal_for_explicit_crash_recovery(self):
        request=Mock(side_effect=[TimeoutError(),(503,{})])
        with patch.object(update,"check_prepared",return_value=(self.folder,self.receipt)),patch.object(update,"inventory",return_value=(self.rows,"physical")),patch.object(update,"json_request",request):
            with self.assertRaisesRegex(RuntimeError,"JournalRetained"):
                update.apply(self.args,self.cfg,self.env)
        journal=self.state/"operator-release-install.json";self.assertTrue(journal.exists())
        with patch.object(update,"native") as native,patch("guardian.processes.Processes"):
            result=update.recover(self.args,self.cfg,self.env)
        self.assertTrue(result["restored"]);self.assertFalse(journal.exists())
        self.assertEqual([("stop",),("start",)],[call.args for call in native.call_args_list])
