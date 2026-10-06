"""Lease starvation and safe SQL protocol rollout regressions."""
import importlib.util
import json
import os
import tempfile
import threading
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from guardian.node import Node
from guardian.sql import SqlStore

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("epoch_repair", ROOT / "deploy/ha/repair_activation_runtime.py")
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class EpochGrantTests(unittest.TestCase):
    def fixture(self, renewal, barrier=0, controller=True, faulted=False):
        calls = []
        count = [0]
        store = SqlStore("comparator")
        connections = []

        @contextmanager
        def connect():
            index = count[0]
            count[0] += 1
            conn = Mock()
            connections.append(conn)
            def execute(sql, *params):
                calls.append((index, sql, params))
                cursor = Mock()
                if "SELECT Token" in sql:
                    cursor.fetchone.return_value = (store.token,) if controller else None
                elif "SELECT Faulted" in sql:
                    cursor.fetchone.return_value = (faulted,)
                elif "OUTPUT inserted.Epoch" in sql:
                    cursor.fetchone.return_value = (9,) if renewal else None
                elif "sp_getapplock" in sql:
                    cursor.fetchone.return_value = (barrier,)
                else:
                    cursor.fetchone.return_value = (1,)
                return cursor
            conn.execute.side_effect = execute
            try:
                yield conn
            finally:
                calls.append((index, "CLOSE", ()))
                conn.close()
        store.connect = connect
        return store, calls, connections

    def test_valid_renewal_never_waits_for_an_exclusive_business_barrier(self):
        store, calls, connections = self.fixture(renewal=True, barrier=-1)
        store.grant("physical")
        self.assertEqual(len(connections), 1)
        self.assertFalse(any("sp_getapplock" in sql or "SET Epoch=" in sql for _, sql, _ in calls))
        update = next(sql for _, sql, _ in calls if "OUTPUT inserted.Epoch" in sql)
        self.assertIn("ExpiresAt>SYSUTCDATETIME()", update)
        connections[0].commit.assert_called_once()

    def test_epoch_transfer_never_occurs_with_an_open_writer_barrier(self):
        store, calls, connections = self.fixture(renewal=False, barrier=-1)
        with self.assertRaisesRegex(RuntimeError, "EpochBarrierUnavailable"):
            store.grant("perimetr")
        self.assertFalse(any("SET Epoch=" in sql for _, sql, _ in calls))
        self.assertEqual(len(connections), 2)
        for conn in connections:
            conn.commit.assert_not_called()
            conn.close.assert_called_once()

    def test_failed_renewal_is_closed_before_acquiring_epoch_barrier(self):
        store, calls, connections = self.fixture(renewal=False)
        store.grant("physical")
        closed = next(i for i, call in enumerate(calls) if call[1] == "CLOSE")
        gate = next(i for i, call in enumerate(calls) if "sp_getapplock" in call[1])
        lease = next(i for i, call in enumerate(calls) if call[0] == 1 and "SELECT Id FROM dbo.KPP_HA_Lease" in call[1])
        self.assertLess(closed, gate)
        self.assertLess(gate, lease)
        connections[0].commit.assert_not_called()
        connections[1].commit.assert_called_once()

    def test_expired_controller_and_quarantined_candidate_cannot_renew(self):
        for controller, faulted, error in ((False, False, "ControllerLeaseLost"), (True, True, "CandidateInRepair")):
            store, calls, _ = self.fixture(True, controller=controller, faulted=faulted)
            with self.assertRaisesRegex(RuntimeError, error):
                store.grant("physical")
            self.assertFalse(any("UPDATE dbo.KPP_HA_Lease" in sql for _, sql, _ in calls))


class OperatorPauseTests(unittest.TestCase):
    def node(self, directory):
        node = Node.__new__(Node)
        node.cfg = {"node_id": "physical", "state_dir": str(directory), "verify_sec": 60}
        node.store = Mock()
        node.store.lease.return_value = {"owner": None, "valid": False, "epoch": 9}
        node.store.node_state.return_value = {"faulted": True}
        node.processes = Mock(children={})
        node.processes.alive.return_value = False
        node.updates = Mock()
        node.updates.state = {"current": {"sha": "a" * 40}, "pending": False}
        node.updates.main_sha = None
        node.updates.activate.return_value = False
        node.stop = threading.Event()
        node.mutation = node.lock = threading.RLock()
        node.maintenance = node.operator_maintenance = False
        node.operator_path = Path(directory) / "operator-maintenance.json"
        node.repair_path = Path(directory) / "repair-verification.json"
        node.repair_attempted = True
        node.preflight_result = {"ok": True}
        node.preflight_at = node.last_sample = time.monotonic()
        node.healthy_since = None
        node.verified_since = time.monotonic() - 120
        node.telemetry = Mock()
        node.status = {"active": True, "healthy": True, "prepared": True}
        return node

    def test_paused_node_cannot_clear_its_quarantine_or_activate_an_update(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.node(directory)
            node.operator_maintenance = True
            node.tick()
            node.store.recovered.assert_not_called()
            node.updates.activate.assert_not_called()
            self.assertFalse(node.snapshot()["prepared"])

    def test_active_node_cannot_be_paused_by_operator_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.node(directory)
            node.store.lease.return_value.update(owner="physical", valid=True)
            with self.assertRaisesRegex(RuntimeError, "ActiveNodeRepairForbidden"):
                node.set_operator_maintenance(True, "a" * 40)
            node.store.begin_repair.assert_not_called()
            node.processes.stop.assert_not_called()
            self.assertFalse(node.operator_path.exists())

    def test_unpause_starts_independent_verification_without_claiming_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.node(directory)
            node.operator_maintenance = True
            node.set_operator_maintenance(False, "a" * 40)
            node.store.recovered.assert_not_called()
            self.assertIsNone(node.verified_since)
            self.assertTrue(node.repair_attempted)
            self.assertEqual(json.loads(node.repair_path.read_text()), {"required": True})

    @patch.dict(os.environ, {"PRIVATE_PASSWORD": "very-secret-value"})
    def test_read_only_diagnostics_work_on_active_node_and_redact_private_values(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.node(directory)
            node.store.lease.return_value.update(owner="physical", valid=True)
            node.processes.children = {"RfidReader": Mock(poll=Mock(return_value=None))}
            path = Path(directory) / "logs/RfidReader.log"
            path.parent.mkdir()
            path.write_text("password=very-secret-value\ntraceback: failure\n", encoding="utf-8")
            result = node.diagnostics()
            self.assertIn("failure", result["logs"]["RfidReader"])
            self.assertNotIn("very-secret-value", result["logs"]["RfidReader"])
            self.assertTrue(result["workers"]["RfidReader"]["running"])
            node.processes.stop.assert_not_called()
            node.store.lease.assert_not_called()
            self.assertFalse(node.maintenance)


class RolloutTests(unittest.TestCase):
    def test_perimetr_handoff_waits_for_valid_comparator_without_claiming_its_lease(self):
        store = MagicMock()
        conn = store.connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.side_effect = [("perimetr", True), ("comparator", False), ("comparator", True)]
        with patch.object(repair.time, "sleep"):
            repair.wait_comparator(store)
        store.claim_controller.assert_not_called()
        store.grant.assert_not_called()
        self.assertEqual(conn.execute.call_count, 3)

    def test_mixed_versions_never_pass_the_sql_migration_gate(self):
        cfg = {"nodes": [{"id": "physical", "url": "http://physical"},
                         {"id": "perimetr", "url": "http://perimetr"}]}
        def status(url, *args, **kwargs):
            node = "perimetr" if "perimetr" in url else "physical"
            return 200, {"node": node, "release_sha": "a" * 40, "sample_age": 0,
                         "fencing_protocol": 1 if node == "perimetr" else 2,
                         "operator_maintenance": True}
        store = Mock()
        with self.assertRaisesRegex(repair.Abort, "Stage the new release"):
            repair.finalize(cfg, store, status, "private-token", "a" * 40, ROOT)
        store.connect.assert_not_called()

    def test_staging_is_not_accepted_as_full_operational_readiness(self):
        status = {"node": "physical", "release_sha": "a" * 40, "fencing_protocol": 2,
                  "sample_age": 0, "active": False, "healthy": False,
                  "prepared": False, "operator_maintenance": True}
        self.assertTrue(repair.staged_status(200, status, "physical", "a" * 40))
        self.assertFalse(status["healthy"])


if __name__ == "__main__":
    unittest.main()
