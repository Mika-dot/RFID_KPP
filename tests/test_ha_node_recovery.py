import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from guardian.node import Node


class NodeRecoveryTests(unittest.TestCase):
    def fixture(self, folder, trial):
        node=Node.__new__(Node)
        node.cfg={"node_id":"physical","verify_sec":60}
        node.store=Mock()
        node.store.lease.return_value={"owner":"perimetr","valid":True,"epoch":2}
        node.store.node_state.return_value={"faulted":True}
        node.processes=Mock(children={})
        node.processes.alive.return_value=False
        node.updates=Mock()
        node.updates.state={"pending":True,"trial_started":trial,"activated_at":time.time()}
        node.updates.activate.return_value=False
        node.stop=threading.Event()
        node.mutation=node.lock=threading.RLock()
        node.maintenance=False
        node.preflight_result={"ok":True,"checks":{"ports_free":True,"sdk_load":True}}
        node.preflight_at=time.monotonic()
        node.healthy_since=None
        node.verified_since=time.monotonic()-61
        node.repair_attempted=True
        node.repair_path=Path(folder)/"repair.json"
        node.rate_path=Path(folder)/"repair-rate.json"
        node.telemetry=Mock()
        return node

    def test_rebuilt_environment_survives_original_repair_quarantine(self):
        with tempfile.TemporaryDirectory() as folder:
            node=self.fixture(folder,False)
            node.tick()
            node.updates.rollback.assert_not_called()
            node.store.recovered.assert_called_once_with("physical")
            self.assertFalse(node.stop.is_set())

    def test_failed_actual_trial_rolls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            node=self.fixture(folder,True)
            node.repair_attempted=False
            node.tick()
            node.updates.rollback.assert_called_once()
            self.assertTrue(node.stop.is_set())

    def test_passive_preflight_arms_recovery_without_controller_or_model(self):
        with tempfile.TemporaryDirectory() as folder:
            node = self.fixture(folder, False)
            node.repair_attempted = False
            node.arm_passive_verification()
            node.store.begin_repair.assert_called_once_with("physical")
            node.store.recovered.assert_not_called()
            node.store.grant.assert_not_called()
            node.store.claim_controller.assert_not_called()
            self.assertTrue(node.repair_attempted)
            node.tick()
            node.store.recovered.assert_not_called()
            node.verified_since = time.monotonic()-61
            node.tick()
            node.store.recovered.assert_called_once_with("physical")

    def test_fault_clear_requires_continuous_fresh_preflight(self):
        with tempfile.TemporaryDirectory() as folder:
            node = self.fixture(folder, False)
            node.preflight_result = {"ok": False}
            node.tick()
            self.assertIsNone(node.verified_since)
            node.preflight_result = {"ok": True}
            node.tick()
            node.store.recovered.assert_not_called()

    def test_auto_verification_excludes_owner_operator_and_failed_trial(self):
        with tempfile.TemporaryDirectory() as folder:
            for case in ("owner", "operator", "trial", "stale", "unhealthy", "children", "active_probe", "dll"):
                with self.subTest(case=case):
                    node = self.fixture(folder, False)
                    node.repair_attempted = False
                    if case == "owner":node.store.lease.return_value["owner"] = "physical"
                    if case == "operator":node.operator_maintenance = True
                    if case == "trial":node.updates.state["trial_started"] = True
                    if case == "stale":node.preflight_at = time.monotonic()-91
                    if case == "unhealthy":node.preflight_result = {"ok":False}
                    if case == "children":node.processes.children = {"worker":Mock()}
                    if case == "active_probe":node.preflight_result["checks"].pop("ports_free")
                    if case == "dll":node.preflight_result["checks"]["sdk_load"] = False
                    node.arm_passive_verification()
                    node.store.begin_repair.assert_not_called()
                    self.assertFalse(node.repair_attempted)

    def test_concurrent_lease_acquisition_does_not_arm_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            node = self.fixture(folder, False)
            node.repair_attempted = False
            node.store.begin_repair.side_effect = RuntimeError("ActiveNodeRepairForbidden")
            with self.assertRaises(RuntimeError):node.arm_passive_verification()
            self.assertFalse(node.repair_attempted)
            self.assertFalse(node.repair_path.exists())

    def test_repeated_fault_keeps_cooldown_across_agent_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            node = self.fixture(folder, False)
            node.repair_attempted = False
            node.arm_passive_verification()
            restarted = self.fixture(folder, False)
            restarted.repair_attempted = False
            restarted.arm_passive_verification()
            restarted.store.begin_repair.assert_not_called()
            self.assertFalse(restarted.repair_attempted)

    def test_sql_failure_during_auto_verification_does_not_kill_probe_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            node = self.fixture(folder, False)
            node.check = Mock()
            node.arm_passive_verification = Mock(side_effect=TimeoutError())
            with patch.object(node.stop,"wait",side_effect=lambda _:node.stop.set()):
                node.probe_loop()
            node.processes.reap_bridges.assert_called_once_with(orphaned_only=True)
            node.telemetry.event.assert_called_once_with("passive_verification_deferred",error="TimeoutError")

    def test_slow_probe_cannot_certify_a_different_release(self):
        with tempfile.TemporaryDirectory() as folder:
            node = self.fixture(folder, False)
            node.cfg["root"] = "/old"
            original = node.preflight_result
            def changed(cfg, store, active):
                node.cfg["root"] = "/new"
                return {"ok":True}
            with patch("guardian.node.preflight", side_effect=changed):
                self.assertFalse(node.check()["ok"])
            self.assertIs(original, node.preflight_result)
