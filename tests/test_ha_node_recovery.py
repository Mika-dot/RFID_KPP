import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

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
        node.preflight_result={"ok":True}
        node.preflight_at=time.monotonic()
        node.healthy_since=None
        node.verified_since=time.monotonic()-61
        node.repair_attempted=True
        node.repair_path=Path(folder)/"repair.json"
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
