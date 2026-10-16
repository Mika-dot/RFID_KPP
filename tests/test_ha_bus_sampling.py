"""Regressions for factory mirror waits, relative spools and trial evidence."""
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from guardian.node import Node
from guardian.probation import BusinessProbation


class BusSamplingTests(unittest.TestCase):
    def node(self, root):
        node = Node.__new__(Node)
        node.cfg = {"node_id": "physical", "root": str(root), "nodes": []}
        node.lock = node.mutation = threading.RLock()
        node.processes = Mock(epoch=3, children={"worker": Mock()})
        node.processes.alive.return_value = True
        node.fallback = node.replica = None
        node.bus_metrics, node.bus_metrics_at = {}, 0
        node.bus_sample_errors = {}
        node.stop = threading.Event()
        node.telemetry = Mock()
        return node

    def spool(self, path, table, pending=2, sent=1):
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(f"CREATE TABLE {table}(state TEXT, created_at TEXT)")
            db.executemany(f"INSERT INTO {table} VALUES(?,?)",
                [(state, "2026-10-15T06:00:00")
                 for state, count in (("PENDING", pending), ("SENT", sent))
                 for _ in range(count)])

    def test_relative_paths_follow_worker_root_not_supervisor_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "release"
            self.spool(root / "queues/rfid.sqlite", "reads")
            self.spool(root / "queues/video.sqlite", "events", 0, 4)
            node = self.node(root)
            node.cfg["env"] = {"RFID_SPOOL_PATH": "queues/rfid.sqlite",
                               "RFID_VIDEO_SPOOL": "queues/video.sqlite"}
            with patch.dict(os.environ, {}, clear=True):
                node.sample_bus()
            self.assertEqual(node.bus_sample_errors, {})
            self.assertEqual(node.bus_metrics["rfid_pending"], 2)
            self.assertEqual(node.bus_metrics["video_sent"], 4)
            self.assertNotEqual(root, Path.cwd())

    def test_missing_spool_is_reported_and_never_created_or_counted_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            node = self.node(Path(tmp))
            node.sample_bus()
            self.assertEqual(node.bus_sample_errors,
                             {"rfid": "SpoolMissing", "video": "SpoolMissing"})
            self.assertEqual(node.bus_metrics, {})
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_queue_samples_progress_while_replication_is_blocked_in_mirror(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            self.spool(root / "rfid_spool_v4.sqlite", "reads")
            self.spool(root / "recordings/video_spool_v3.sqlite", "events")
            node = self.node(root)
            entered, release = threading.Event(), threading.Event()
            def blocked():
                entered.set()
                release.wait(5)
                node.stop.set()
            node.sync_mirror = blocked
            thread = threading.Thread(target=node.replication_loop)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                node.sample_bus()
                self.assertEqual(node.bus_metrics["rfid_sent"], 1)
                with closing(sqlite3.connect(root / "rfid_spool_v4.sqlite")) as db, db:
                    db.execute("UPDATE reads SET state='SENT' WHERE state='PENDING'")
                node.sample_bus()
                self.assertEqual(node.bus_metrics["rfid_pending"], 0)
                self.assertEqual(node.bus_metrics["rfid_sent"], 3)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())

    def test_sample_from_previous_epoch_cannot_overwrite_new_epoch_metrics(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            node = self.node(Path(tmp))
            def changed():
                node.processes.epoch = 4
                return {"records": 2}
            node.fallback = Mock()
            node.fallback.stats.side_effect = changed
            node.sample_bus()
            self.assertEqual(node.bus_metrics_at, 0)
            self.assertEqual(node.bus_metrics, {})

    def active(self, root):
        node = self.node(root)
        node.store = Mock()
        node.store.lease.return_value = {"owner": "physical", "valid": True, "epoch": 3}
        node.store.node_state.return_value = {"faulted": False}
        node.updates = Mock()
        node.updates.state = {"pending": True, "trial_started": True,
                              "trial_started_at": time.time()}
        node.updates.operator_pinned.return_value = True
        node.maintenance = node.operator_maintenance = False
        node.preflight_result, node.preflight_at = {"ok": True}, time.monotonic()
        node.healthy_since = None
        node.business_probation = BusinessProbation(120)
        node.bus_metrics = {"rfid_pending": 20, "rfid_sent": 1,
                            "video_pending": 0, "video_sent": 0}
        node.bus_metrics_at = time.monotonic()
        node.business_probation.assess(node.bus_metrics, time.monotonic()-121)
        return node

    @patch("guardian.node.services_health", return_value={str(i): {"ok": True} for i in range(5)})
    def test_stale_sample_does_not_invent_a_stalled_delivery_or_confirm_trial(self, health):
        node = self.active(Path.cwd())
        node.bus_metrics_at -= 20
        node.tick()
        self.assertTrue(node.status["healthy"])
        self.assertEqual(node.business_probation.streams, {})
        node.telemetry.event.assert_not_called()
        node.updates.confirm.assert_not_called()

    @patch("guardian.node.services_health", return_value={str(i): {"ok": True} for i in range(5)})
    def test_cached_sample_cannot_extend_stall_beyond_last_observed_time(self, health):
        node = self.active(Path.cwd())
        node.bus_metrics_at -= 10  # Last observation covers 111 s, not 121 s.
        node.tick()
        self.assertTrue(node.status["healthy"])
        node.telemetry.event.assert_not_called()

    @patch("guardian.node.services_health", return_value={str(i): {"ok": True} for i in range(5)})
    def test_continuous_fresh_stalled_queue_still_fails_active_candidate(self, health):
        node = self.active(Path.cwd())
        node.tick()
        self.assertFalse(node.status["healthy"])
        node.telemetry.event.assert_called_once_with(
            "candidate_business_fault", reason="candidate_rfid_delivery_stalled")

    @patch("guardian.node.services_health", return_value={str(i): {"ok": True} for i in range(5)})
    def test_fresh_progress_and_empty_queues_are_healthy_without_new_traffic(self, health):
        node = self.active(Path.cwd())
        node.bus_metrics.update(rfid_pending=0, rfid_sent=21)
        node.tick()
        self.assertTrue(node.status["healthy"])
        node.telemetry.event.assert_not_called()

    @patch("guardian.node.services_health", return_value={str(i): {"ok": True} for i in range(5)})
    def test_absent_trial_metrics_still_fail_after_bounded_grace(self, health):
        node = self.active(Path.cwd())
        node.bus_metrics = {}
        node.updates.state["trial_started_at"] -= 241
        node.tick()
        self.assertFalse(node.status["healthy"])
        node.updates.confirm.assert_not_called()
        node.telemetry.event.assert_called_once_with(
            "candidate_business_fault", reason="candidate_business_metrics_unavailable")

    def test_passive_candidate_does_not_diagnose_stopped_workers_as_stalled(self):
        node = self.active(Path.cwd())
        node.store.lease.return_value["owner"] = "perimetr"
        node.processes.children = {}
        node.updates.activate.return_value = False
        node.updates.state["trial_started_at"] -= 241
        node.tick()
        node.telemetry.event.assert_not_called()
        node.updates.confirm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
