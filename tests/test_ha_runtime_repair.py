"""Safety checks for operator-driven repair while HA is enabled."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime_repair", ROOT / "deploy/ha/repair_activation_runtime.py")
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class RepairSafetyTests(unittest.TestCase):
    def resume_setup(self, released=False):
        cfg = {"nodes": [{"id": n, "priority": i, "url": "http://" + n}
                         for i, n in enumerate(("physical", "perimetr", "comparator"), 1)]}
        store = MagicMock()
        store.lease.return_value = {"enabled": True}
        conn = store.connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.return_value = (0, "Perimeter.HA.Epoch READCOMMITTEDLOCK")
        def get(url, token, **kw):
            if url.endswith("/status"):
                return 200, {"node": url.split("/")[2], "release_sha": "candidate", "fencing_protocol": 2,
                             "sample_age": 1, "active": False, "operator_maintenance": not released,
                             "preflight": {"ok": True}}
            if url.endswith("/diagnostics"):
                return 200, {"workers": {}}
            return 200, {"operator_maintenance": False}
        return cfg, store, conn, Mock(side_effect=get)

    def test_resume_uses_only_existing_selects_and_guarded_release_no_migration(self):
        cfg, store, conn, get = self.resume_setup()
        with patch.object(repair, "wait_cluster") as wait, patch("builtins.print"):
            repair.resume_protocol2(cfg, store, get, "private-token", "candidate")
        self.assertEqual(len(conn.execute.call_args_list), 9)
        self.assertTrue(all(c.args[0].startswith("SELECT ") for c in conn.execute.call_args_list))
        posts = [c for c in get.call_args_list if "body" in c.kwargs]
        self.assertEqual([c.args[0] for c in posts], ["http://physical/maintenance", "http://perimetr/maintenance",
                                                   "http://comparator/maintenance"])
        self.assertTrue(all(c.kwargs["body"] == {"enabled": False, "release": "candidate"} for c in posts))
        store.grant.assert_not_called()
        store.fault.assert_not_called()
        conn.commit.assert_not_called()
        wait.assert_called_once()

    def test_resume_refuses_disabled_missing_or_incompatible_trigger_before_release(self):
        for row in (None, (1, "Perimeter.HA.Epoch READCOMMITTEDLOCK"), (0, "UPDLOCK")):
            cfg, store, conn, get = self.resume_setup()
            conn.execute.return_value.fetchone.return_value = row
            with self.assertRaises(repair.Abort):
                repair.resume_protocol2(cfg, store, get, "token", "candidate")
            get.assert_not_called()

    def test_resume_requires_all_three_exact_releases_and_empty_held_workers_before_release(self):
        for bad in ("release", "workers", "preflight"):
            cfg, store, conn, get = self.resume_setup()
            original = get.side_effect
            def responses(url, token, **kw):
                code, value = original(url, token, **kw)
                if url == "http://comparator/status" and bad == "release":
                    value["release_sha"] = "old"
                if url == "http://comparator/status" and bad == "preflight":
                    value["preflight"]["ok"] = False
                if url == "http://comparator/diagnostics" and bad == "workers":
                    value["workers"] = {"RfidReader": {"running": True}}
                return code, value
            get.side_effect = responses
            with self.assertRaises(repair.Abort):
                repair.resume_protocol2(cfg, store, get, "token", "candidate")
            self.assertFalse(any("body" in c.kwargs for c in get.call_args_list))

    def test_resume_retry_does_not_toggle_already_released_maintenance(self):
        cfg, store, conn, get = self.resume_setup(released=True)
        with patch.object(repair, "wait_cluster"), patch("builtins.print"):
            repair.resume_protocol2(cfg, store, get, "token", "candidate")
        self.assertFalse(any("body" in c.kwargs for c in get.call_args_list))

    def test_resume_external_ha_off_never_sends_release(self):
        cfg, store, conn, get = self.resume_setup()
        store.lease.return_value = {"enabled": False}
        with self.assertRaises(repair.Abort):
            repair.resume_protocol2(cfg, store, get, "token", "candidate")
        get.assert_not_called()

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
