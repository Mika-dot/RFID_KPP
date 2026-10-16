import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from guardian.node import Node
from guardian.repair import LmRepair, redact, validate_step


class RepairTests(unittest.TestCase):
    def test_valid_step(self):
        self.assertEqual("verify", validate_step({"action":"verify", "service":"all", "reason":"probe"})["action"])

    def test_shell_injection_rejected(self):
        for action in ("bash -c rm -rf /", "promote", "sql", "recovered", "exec", "cmd"):
            with self.assertRaises(ValueError):
                validate_step({"action":action, "service":"all", "reason":"a"})

    def test_extra_shell_fields_rejected(self):
        with self.assertRaises(ValueError):
            validate_step({"action":"verify", "service":"all", "reason":"a", "command":"shutdown"})

    def test_fake_service_rejected(self):
        with self.assertRaises(ValueError):
            validate_step({"action":"restart_service", "service":"Zabbix Agent", "reason":"a"})

    def test_secret_redaction(self):
        s = redact("PWD=abc;UID=admin; password=hunter2 rtsp://user:pass@host/stream token=secret")
        for value in ("abc", "admin", "hunter2", "user:pass", "secret"):
            self.assertNotIn(value, s)

    def test_active_node_repair_forbidden(self):
        node = Node.__new__(Node)
        node.cfg = {"node_id":"physical"}
        node.mutation = threading.RLock()
        node.store = Mock()
        node.store.lease.return_value = {"owner":"physical", "valid":True}
        with self.assertRaisesRegex(RuntimeError, "ActiveNodeRepairForbidden"):
            node.repair("rollback_release", "all")

    def test_verified_flag_does_not_come_from_model(self):
        with tempfile.TemporaryDirectory() as d:
            node = Node.__new__(Node)
            node.cfg = {"node_id":"physical"}
            node.mutation = threading.RLock()
            node.store, node.processes = Mock(), Mock()
            node.store.lease.return_value = {"owner":"perimetr", "valid":True}
            node.rate_path = Path(d)/"rate.json"
            node.repair_path = Path(d)/"verify.json"
            node.check = Mock(return_value={"ok":True})
            result = node.repair("restart_service", "all")
            self.assertFalse(result["verified"])
            node.store.recovered.assert_not_called()

    @patch("guardian.repair.get_json")
    def test_model_selection_skips_small_and_ocr_models(self, get):
        get.side_effect = [(200,{"data":[{"id":"qwen_qwen3.5-0.8b"}, {"id":"glm-ocr"},
                                          {"id":"openai/gpt-oss-20b"}]}),
                           (200,{"choices":[{"finish_reason":"stop", "message":{"content":json.dumps(
                               {"action":"verify", "service":"all", "reason":"check"})}}]})]
        worker = LmRepair({}, Mock(), Mock(), threading.Event())
        self.assertEqual("verify", worker.step({})["action"])
        self.assertEqual("openai/gpt-oss-20b", get.call_args.kwargs["body"]["model"])

    @patch("guardian.repair.get_json")
    def test_truncated_json_is_never_executed(self, get):
        get.side_effect = [(200,{"data":[{"id":"openai/gpt-oss-20b"}]}),
                          (200,{"choices":[{"finish_reason":"length", "message":{"content":"{}"}}]})]
        with self.assertRaisesRegex(RuntimeError, "NoValidatedRepairModel"):
            LmRepair({}, Mock(), Mock(), threading.Event()).step({})

    @patch("guardian.repair.get_json")
    def test_bad_primary_falls_back_to_validated_8b(self, get):
        get.side_effect = [(200,{"data":[{"id":"openai/gpt-oss-20b"}, {"id":"qwen/qwen3-vl-8b"}]}),
                           (200,{"choices":[{"finish_reason":"stop", "message":{"content":"not json"}}]}),
                           (200,{"choices":[{"finish_reason":"stop", "message":{"content":json.dumps(
                               {"action":"wait", "service":"all", "reason":"need logs"})}}]})]
        self.assertEqual("wait", LmRepair({}, Mock(), Mock(), threading.Event()).step({})["action"])

    def test_repair_loop_never_claims_or_renews_controller_lease(self):
        store, stop = Mock(), Mock()
        store.controller_owned.return_value = False
        stop.wait.side_effect = [False, True]
        LmRepair({}, store, Mock(), stop).run()
        store.controller_owned.assert_called_once()
        store.claim_controller.assert_not_called()

    @patch.dict("os.environ", {"PERIMETER_HA_TOKEN":"test"})
    @patch("guardian.repair.get_json")
    def test_late_model_response_discarded_after_controller_handoff(self, get):
        store = Mock()
        store.lease.return_value = {"owner":"perimetr", "valid":True}
        store.node_state.return_value = {"faulted":True}
        store.controller_owned.return_value = True
        get.side_effect = [(200, {"active":False}), (200, {"preflight":{"ok":False}})]
        worker = LmRepair({}, store, Mock(), threading.Event())
        def delayed(evidence):
            store.controller_owned.return_value = False
            return {"action":"repair_dependencies", "service":"all", "reason":"fix"}
        worker.step = delayed
        worker.repair_node({"id":"physical", "url":"http://physical"})
        self.assertEqual(2, get.call_count)
        store.claim_controller.assert_not_called()

    @patch.dict("os.environ", {"PERIMETER_HA_TOKEN":"test"})
    @patch("guardian.repair.get_json")
    def test_prepared_repair_does_not_wait_for_models_or_repeat_restart(self, get):
        store = Mock()
        store.lease.return_value = {"owner":"physical", "valid":True}
        store.controller_owned.return_value = True
        get.side_effect = [(200, {"active":False}), (200, {"preflight":{"ok":True}})]
        worker = LmRepair({}, store, Mock(), threading.Event())
        worker.step = Mock(side_effect=AssertionError("LM must not delay independent verification"))
        worker.repair_node({"id":"comparator", "url":"http://comparator"})
        worker.step.assert_not_called()
        self.assertEqual(2, get.call_count)
        store.recovered.assert_not_called()

    @patch.dict("os.environ", {"PERIMETER_HA_TOKEN":"test"})
    @patch("guardian.repair.get_json")
    def test_rejected_repair_does_not_invoke_model(self, get):
        store = Mock()
        store.lease.return_value = {"owner":"physical", "valid":True}
        store.controller_owned.return_value = True
        get.side_effect = [(200, {"active":False}), (409, {"error":"OperatorMaintenance"})]
        worker = LmRepair({}, store, Mock(), threading.Event())
        worker.step = Mock()
        worker.repair_node({"id":"comparator", "url":"http://comparator"})
        worker.step.assert_not_called()

    @patch("guardian.repair.subprocess.run")
    def test_expired_controller_cannot_start_host_rescue(self, run):
        store = Mock()
        store.controller_owned.return_value = False
        worker = LmRepair({}, store, Mock(), threading.Event())
        worker.rescue({"id":"physical", "rescue_argv":["ssh", "fixed-host", "fixed-command"]})
        run.assert_not_called()
