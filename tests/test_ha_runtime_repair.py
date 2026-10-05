"""Safety checks for operator-driven repair while HA is enabled."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime_repair", ROOT / "deploy/ha/repair_activation_runtime.py")
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class RepairSafetyTests(unittest.TestCase):
    def test_disabled_ha_is_rejected_without_faulting_any_node(self):
        store = Mock()
        store.lease.return_value = {"enabled": False}
        with self.assertRaisesRegex(repair.Abort, "requires enabled HA"):
            repair.quarantine(store, "physical")
        store.fault.assert_not_called()
        store.begin_repair.assert_not_called()

    def test_quarantine_waits_for_controller_demotion_before_transactional_claim(self):
        store = Mock()
        calls = []
        leases = iter([
            {"enabled": True, "owner": "physical", "valid": True},
            {"enabled": True, "owner": "physical", "valid": True},
            {"enabled": True, "owner": "perimetr", "valid": True},
        ])
        def lease():
            result = next(leases)
            calls.append(("lease", result["owner"]))
            return result
        store.lease.side_effect = lease
        store.fault.side_effect = lambda node: calls.append(("fault", node))
        store.begin_repair.side_effect = lambda node: calls.append(("begin", node))
        with patch.object(repair.time, "sleep"):
            repair.quarantine(store, "physical")
        self.assertEqual(calls, [("lease", "physical"), ("fault", "physical"),
                                  ("lease", "physical"), ("lease", "perimetr"), ("begin", "physical")])

    def test_owned_lease_timeout_never_claims_repair(self):
        store = Mock()
        store.lease.return_value = {"enabled": True, "owner": "physical", "valid": True}
        with patch.object(repair.time, "monotonic", side_effect=[0, 46]):
            with self.assertRaisesRegex(repair.Abort, "nothing was stopped"):
                repair.quarantine(store, "physical")
        store.begin_repair.assert_not_called()

    def test_reused_pid_is_never_accepted_as_a_captured_process(self):
        psutil = Mock()
        psutil.NoSuchProcess = ProcessLookupError
        psutil.Process.return_value.create_time.return_value = 200.0
        self.assertFalse(repair.process_alive(psutil, (123, 100.0)))


if __name__ == "__main__":
    unittest.main()
