import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import psutil

from guardian.config import atomic_json
from guardian.processes import Processes


@unittest.skipIf(os.name == "nt", "Wine process cleanup is Linux-specific")
class BridgeCleanupTests(unittest.TestCase):
    def fixture(self, folder, node="physical", worker="wine_worker.py"):
        proc = subprocess.Popen([sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(30)", worker],
            env=dict(os.environ, PERIMETER_HA_NODE=node), start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.addCleanup(self.clean, proc)
        proc.stdout.readline()
        try:
            observed = psutil.Process(proc.pid)
        except psutil.NoSuchProcess:
            if proc.poll() is None and not os.getenv("GITHUB_ACTIONS"):
                self.skipTest("Execution environment exposes a different /proc PID namespace")
            raise
        if worker not in observed.cmdline():
            self.assertFalse(os.getenv("GITHUB_ACTIONS"), "CI must expose its own process namespace")
            self.skipTest("Execution environment exposes a different /proc PID namespace")
        try:
            observed.environ()
        except psutil.AccessDenied:
            self.assertFalse(os.getenv("GITHUB_ACTIONS"), "CI must allow child process inspection")
            self.skipTest("Execution environment denies child process environment access")
        path = Path(folder) / "wine-bridges" / (str(proc.pid) + ".json")
        atomic_json(path, {"pid":proc.pid, "created":psutil.Process(proc.pid).create_time()})
        return proc, path, Processes({"state_dir":folder, "node_id":"physical"})

    @staticmethod
    def clean(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        proc.stdout.close()

    def test_bridge_is_stopped_even_when_rfid_parent_is_already_gone(self):
        with tempfile.TemporaryDirectory() as folder:
            proc, path, processes = self.fixture(folder)
            self.assertFalse(processes.children)
            processes.stop()
            self.assertIsNotNone(proc.poll())
            self.assertFalse(path.exists())

    def test_stale_pid_record_does_not_kill_reused_process(self):
        with tempfile.TemporaryDirectory() as folder:
            proc, path, processes = self.fixture(folder)
            atomic_json(path, {"pid":proc.pid, "created":0})
            processes.stop()
            self.assertIsNone(proc.poll())

    def test_other_node_process_is_not_killed(self):
        with tempfile.TemporaryDirectory() as folder:
            proc, path, processes = self.fixture(folder, node="perimetr")
            processes.stop()
            self.assertIsNone(proc.poll())

    def test_unrelated_command_is_not_killed(self):
        with tempfile.TemporaryDirectory() as folder:
            proc, path, processes = self.fixture(folder, worker="unrelated.py")
            processes.stop()
            self.assertIsNone(proc.poll())

    def test_orphan_cleanup_preserves_live_parent_bridge(self):
        with tempfile.TemporaryDirectory() as folder:
            proc, path, processes = self.fixture(folder)
            atomic_json(path, {"pid":proc.pid, "created":psutil.Process(proc.pid).create_time(),
                              "parent_pid":os.getpid(), "parent_created":psutil.Process().create_time()})
            processes.reap_bridges(orphaned_only=True)
            self.assertIsNone(proc.poll())
            self.assertTrue(path.exists())

    def test_orphan_cleanup_reaps_dead_parent_bridge(self):
        with tempfile.TemporaryDirectory() as folder:
            proc, path, processes = self.fixture(folder)
            atomic_json(path, {"pid":proc.pid, "created":psutil.Process(proc.pid).create_time(),
                              "parent_pid":os.getpid(), "parent_created":0})
            processes.reap_bridges(orphaned_only=True)
            self.assertIsNotNone(proc.poll())
            self.assertFalse(path.exists())
