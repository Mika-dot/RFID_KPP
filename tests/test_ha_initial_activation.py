import importlib.util
import io
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


PATH = Path(__file__).resolve().parents[1] / "deploy/ha/activate_initial.py"
SPEC = importlib.util.spec_from_file_location("initial_activation", PATH)
activate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activate)


class ActivationGuardsTests(unittest.TestCase):
    def agent(self):
        return {"node": "perimetr", "sample_age": 1, "release_sha": activate.RELEASE,
                "prepared": True, "resources": {"restart_required": False}}

    def test_priority_is_executor_order_not_controller_order(self):
        cfg = {"node_id": "comparator", "controller_enabled": True,
               "nodes": [{"id": node, "priority": rank} for node, rank in activate.PRIORITY]}
        activate.check_priority(cfg)
        cfg["nodes"][0]["priority"], cfg["nodes"][2]["priority"] = 3, 1
        with self.assertRaises(activate.Abort):
            activate.check_priority(cfg)

    def test_readiness_rejects_wrong_identity_stale_release_and_resource_fault(self):
        for key, value in (("node", "physical"), ("sample_age", 12),
                           ("release_sha", "old"), ("prepared", False),
                           ("resources", {"restart_required": True})):
            with self.subTest(key=key):
                data = self.agent()
                data[key] = value
                with self.assertRaises(activate.Abort):
                    activate.check_agent("perimetr", 200, data, True)
        with self.assertRaises(activate.Abort):
            activate.check_agent("perimetr", 503, self.agent(), True)

    def test_guardian_old_venv_is_not_a_legacy_worker(self):
        root = "D:/Desktop/RFID_KPP-main"
        args = [root + "/venv64/Scripts/python.exe", "-m", "guardian",
                "--config", "D:/PerimeterHA/node.json"]
        self.assertFalse(activate.legacy_command("python.exe", args, root))
        args = [root + "/venv64/Scripts/python.exe", "D:/PerimeterHA/source/guardian/boot.py"]
        self.assertFalse(activate.legacy_command("python.exe", args, root))
        args = [root + "/venv64/Scripts/python.exe", root + "/deploy/run_service.py"]
        self.assertTrue(activate.legacy_command("python.exe", args, root))

    def test_operator_source_and_similar_folder_are_excluded(self):
        root = "D:/Desktop/RFID_KPP-main"
        self.assertFalse(activate.legacy_command("python.exe", ["python.exe", "-c", root + "/deploy/x.py"], root, True))
        self.assertFalse(activate.legacy_command("python.exe", ["python.exe", root + "-copy/deploy/x.py"], root))
        self.assertFalse(activate.legacy_command("powershell.exe", ["powershell.exe", root + "/deploy/x.ps1"], root))
        self.assertTrue(activate.legacy_command("CMD.EXE", ["cmd.exe", "/K", '"d:\\desktop\\rfid_kpp-main\\deploy\\RUN_WEB_V3.cmd"'], root))

    def test_pid_reuse_is_not_a_survivor(self):
        import psutil
        with patch.object(psutil, "Process", return_value=SimpleNamespace(create_time=lambda: 200)):
            self.assertFalse(activate.same_process(psutil, (12, 100)))
            self.assertTrue(activate.same_process(psutil, (12, 200)))
        with patch.object(psutil, "Process", side_effect=psutil.NoSuchProcess(12)):
            self.assertFalse(activate.same_process(psutil, (12, 100)))

    def fake_tree(self):
        process = Mock()
        process.pid = 10
        process.create_time.return_value = 100
        process.cmdline.return_value = ["cmd.exe", "old-wrapper.cmd"]
        child = Mock()
        child.pid = 11
        child.create_time.return_value = 101
        process.children.return_value = [child]
        return process

    def test_nonzero_taskkill_accepted_only_when_tree_disappears(self):
        import psutil
        process = self.fake_tree()
        with patch.object(psutil, "Process", return_value=process), \
             patch.object(activate, "same_process", side_effect=[True, False, False]) as identities, \
             patch.object(activate.subprocess, "run", return_value=SimpleNamespace(returncode=1)), \
             redirect_stdout(io.StringIO()) as output:
            activate.stop_tree(psutil, process)
        self.assertIn("taskkill_exit=1", output.getvalue())
        self.assertEqual([(10, 100), (11, 101), (10, 100)], [c.args[1] for c in identities.call_args_list])

    def test_even_zero_taskkill_rejected_when_worker_survives(self):
        import psutil
        process = self.fake_tree()
        with patch.object(psutil, "Process", return_value=process), \
             patch.object(activate, "same_process", return_value=True), \
             patch.object(activate.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
             patch.object(activate.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaisesRegex(activate.Abort, "remains"):
                activate.stop_tree(psutil, process)

    def test_changed_pid_is_not_killed(self):
        import psutil
        with patch.object(activate, "same_process", return_value=False), \
             patch.object(activate.subprocess, "run") as kill:
            with self.assertRaisesRegex(activate.Abort, "changed"):
                activate.stop_tree(psutil, self.fake_tree())
            kill.assert_not_called()

    def cluster(self):
        cluster = object.__new__(activate.Cluster)
        cluster.store = Mock()
        cluster.restore_legacy = Mock()
        cluster.nodes = {name: {} for name, _ in activate.PRIORITY}
        return cluster

    def test_uncertain_or_enabled_lease_never_restarts_old_writers(self):
        for lease in (None, {"enabled": True, "owner": "perimetr"},
                      {"enabled": False, "owner": "physical"}):
            with self.subTest(lease=lease):
                cluster = self.cluster()
                if lease is None:
                    cluster.store.lease.side_effect = TimeoutError()
                else:
                    cluster.store.lease.return_value = lease
                with redirect_stdout(io.StringIO()) as output:
                    cluster.recover_after_failure(None, None)
                cluster.restore_legacy.assert_not_called()
                self.assertIn("DO_NOT_START_LEGACY", output.getvalue())

    def test_fresh_disabled_unowned_lease_allows_guarded_restore(self):
        cluster = self.cluster()
        cluster.store.lease.return_value = {"enabled": False, "owner": None}
        cluster.recover_after_failure("process-api", "root")
        cluster.restore_legacy.assert_called_once_with("process-api", "root")

    def test_passive_gate_rejects_a_running_or_faulted_agent(self):
        for field in ("active", "faulted"):
            cluster = self.cluster()
            cluster.store.lease.return_value = {"enabled": False, "owner": None}
            state = {"active": False, "faulted": False}
            state[field] = True
            cluster.status = Mock(return_value=deepcopy(state))
            with self.assertRaises(activate.Abort):
                cluster.passive()

    def test_restore_never_launches_over_an_orphan(self):
        cluster = self.cluster()
        cluster.passive = Mock()
        cluster.launchers = Mock(return_value={})
        cluster.legacy_processes = Mock(return_value=["orphan"])
        with patch.object(activate.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(activate.Abort, "orphan"):
                activate.Cluster.restore_legacy(cluster, None, Path("root"))
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
