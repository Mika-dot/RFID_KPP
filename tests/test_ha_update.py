import tempfile
import os
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from guardian.update import Updates
from guardian.release_contract import BASE_FLOOR, MANIFEST


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {"root":self.tmp.name, "state_dir":self.tmp.name, "release_sha":"a"*40,
                    "update_source":self.tmp.name,"release_dir":self.tmp.name, "python":"python"}
        with patch.object(Updates,"git",return_value="a"*40):
            self.updates = Updates(self.cfg,Mock(),Mock())

    def test_pending_release_not_replaced_again(self):
        self.updates.staged={"root":"/new","sha":"b"*40}
        self.assertTrue(self.updates.activate())
        self.updates.staged={"root":"/third","sha":"c"*40}
        self.assertFalse(self.updates.activate())
        self.assertEqual("b"*40,self.updates.state["current"]["sha"])

    def test_rollback_preserves_config_and_durable_state(self):
        spool=Path(self.tmp.name)/"rfid_spool_v4.sqlite"
        spool.write_bytes(b"durable data")
        self.updates.staged={"root":"/new","sha":"b"*40}
        self.updates.activate()
        self.updates.rollback()
        self.assertEqual("a"*40,self.updates.state["current"]["sha"])
        self.assertEqual(b"durable data",spool.read_bytes())
        self.assertIn("b"*40,self.updates.quarantined)

    def test_rejected_commit_survives_agent_restart(self):
        self.updates.staged={"root":"/new","sha":"b"*40}
        self.updates.activate()
        self.updates.rollback()
        restored=Updates(self.cfg,Mock(),Mock())
        self.assertIn("b"*40,restored.quarantined)

    def test_commit_is_confirmed_only_after_independent_verify(self):
        self.updates.staged={"root":"/new","sha":"b"*40}
        self.updates.activate()
        self.assertTrue(self.updates.state["pending"])
        self.assertFalse(self.updates.state["trial_started"])
        self.updates.begin_trial()
        self.assertTrue(self.updates.state["trial_started"])
        self.updates.confirm()
        self.assertFalse(self.updates.state["pending"])

    def test_no_previous_release_refuses_rollback(self):
        with self.assertRaisesRegex(RuntimeError,"NoPreviousRelease"):
            self.updates.rollback()

    @patch("guardian.update.subprocess.run")
    def test_transient_live_preflight_retries_same_sha_after_agent_restart(self, run):
        def qualified(command, **kwargs):
            if "--report" in command:
                Path(command[command.index("--report")+1]).write_text(json.dumps({"status":"passed"}),encoding="utf-8")
            return Mock(returncode=0)
        run.side_effect=qualified
        def git(*args,**kwargs):
            if args==("rev-parse","FETCH_HEAD"):return "b"*40
            if args==("show","b"*40+":"+MANIFEST):return json.dumps(BASE_FLOOR)
            if args[:2]==("diff","--name-only"):return "common/business_flow.py\n"
            return ""
        self.updates.git=git
        with patch("guardian.update.preflight",return_value={"ok":False,"checks":{"exception":"TimeoutError"}}):
            with self.assertRaisesRegex(RuntimeError,"CandidatePreflightFailed"):
                self.updates.stage()
        self.assertIsNone(self.updates.staged)
        self.assertNotIn("b"*40,self.updates.quarantined)
        self.assertNotIn("b"*40,self.updates.state.get("rejected",[]))
        self.updates.telemetry.event.assert_called_with("update_candidate_deferred",sha="b"*40,reason="CandidatePreflightFailed")
        restored=Updates(self.cfg,Mock(),Mock());restored.git=git
        with patch("guardian.update.preflight",return_value={"ok":True}):
            restored.stage()
        self.assertEqual("b"*40,restored.staged["sha"])

    @patch("guardian.update.subprocess.run")
    def test_protocol_two_deployment_rejects_main_without_epoch_barrier_before_running_candidate(self, run):
        self.updates.state["fencing_protocol_min"] = 2
        def git(*args, **kwargs):
            if args == ("rev-parse", "FETCH_HEAD"):
                return "b" * 40
            if args == ("show", "b" * 40 + ":guardian/sql.py"):
                return "FENCING_PROTOCOL = 1\n"
            return ""
        self.updates.git = git
        with self.assertRaisesRegex(RuntimeError, "CandidateFencingProtocolIncompatible"):
            self.updates.stage()
        run.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Ubuntu reserves use the CPU wheel index")
    @patch("guardian.update.subprocess.run")
    def test_cpu_environment_repair_installs_cpu_torch_before_requirements(self, run):
        run.return_value.returncode = 0
        self.updates.repair_dependencies()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("--index-url", commands[1])
        self.assertEqual("https://download.pytorch.org/whl/cpu", commands[1][-1])
        self.assertIn("-r", commands[2])
        self.assertTrue(self.updates.state["pending"])
        self.assertFalse(self.updates.state["trial_started"])

    @patch("guardian.update.preflight",return_value={"ok":True})
    @patch("guardian.update.subprocess.run")
    def test_squash_merge_checks_main_ancestry_instead_of_feature_ancestry(self, run, preflight):
        run.return_value.returncode=0
        def qualified_command(command, **kwargs):
            if "--report" in command:
                proof = Path(command[command.index("--report")+1])
                proof.write_text(json.dumps({"status":"passed"}), encoding="utf-8")
            return Mock(returncode=0)
        run.side_effect = qualified_command
        self.updates.state["trusted_main_sha"]="d"*40
        calls=[]
        def git(*args,**kwargs):
            calls.append(args)
            if args == ("rev-parse", "FETCH_HEAD"):
                return "b" * 40
            if args == ("show", "b" * 40 + ":" + MANIFEST):
                return json.dumps(BASE_FLOOR)
            if args[:2] == ("diff", "--name-only"):
                return "common/business_flow.py\n"
            return ""
        self.updates.git=git
        self.updates.stage()
        self.assertIn(("merge-base","--is-ancestor","d"*40,"b"*40),calls)
        self.assertNotIn(("merge-base","--is-ancestor","a"*40,"b"*40),calls)
        self.assertEqual("b" * 40, self.updates.staged["sha"])
