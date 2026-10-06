import errno
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from guardian.__main__ import supervise
from guardian.resources import ResourceGuard
from guardian.telemetry import Telemetry


class ResourceTests(unittest.TestCase):
    def test_audit_write_exhaustion_sets_recovery_signal(self):
        telemetry = Telemetry.__new__(Telemetry)
        telemetry.cfg = {"state_dir": "/unused"}
        telemetry.resource_exhausted = threading.Event()
        telemetry.queue = Mock()
        telemetry.queue.get.side_effect = [{"kind": "test"}, StopIteration]
        path = MagicMock()
        path.__truediv__.return_value = path
        path.exists.return_value = False
        path.open.side_effect = OSError(errno.EMFILE, "exhausted")
        with patch("guardian.telemetry.Path", return_value=path), \
             patch("common.observability.init_sentry", return_value=False), \
             self.assertLogs(level="ERROR"), self.assertRaises(StopIteration):
            telemetry._writer()
        self.assertTrue(telemetry.resource_exhausted.is_set())
        telemetry.queue.task_done.assert_called_once_with()

    def test_exhaustion_during_sampling_requires_restart(self):
        guard = ResourceGuard()
        with patch("guardian.resources.os.name", "nt"):
            guard.process = Mock()
            guard.process.num_handles.side_effect = OSError(errno.EMFILE, "exhausted")
            status = guard.sample()
        self.assertTrue(status["restart_required"])
        self.assertEqual(errno.EMFILE, status["error_errno"])

    def test_windows_handle_count_is_reported_without_inventing_a_limit(self):
        guard = ResourceGuard()
        with patch("guardian.resources.os.name", "nt"):
            guard.process = Mock()
            guard.process.num_handles.return_value = 73
            status = guard.sample()
        self.assertEqual(73, status["open_handles"])
        self.assertIsNone(status["fd_limit"])
        self.assertFalse(status["restart_required"])

    def test_live_threads_cannot_hide_descriptor_exhaustion(self):
        stop = threading.Event()
        node = SimpleNamespace(lock=threading.RLock(), resources={})
        telemetry = Mock(resource_exhausted=threading.Event())
        guard = Mock()
        guard.sample.return_value = {"restart_required": True, "open_fds": 900, "fd_limit": 1024}
        threads = [Mock(is_alive=Mock(return_value=True))]
        self.assertTrue(supervise(stop, node, threads, telemetry, guard))
        self.assertTrue(stop.is_set())
        telemetry.event.assert_called_once_with("agent_resource_exhausted",
                                                restart_required=True, open_fds=900, fd_limit=1024)

    def test_audit_exhaustion_requires_restart_even_after_descriptors_are_freed(self):
        stop = threading.Event()
        node = SimpleNamespace(lock=threading.RLock(), resources={})
        exhausted = threading.Event()
        exhausted.set()
        telemetry = Mock(resource_exhausted=exhausted)
        guard = Mock()
        guard.sample.return_value = {"restart_required": False, "open_fds": 12, "fd_limit": 1024}
        self.assertTrue(supervise(stop, node, [Mock()], telemetry, guard))
        self.assertTrue(node.resources["restart_required"])

    def test_resource_metrics_include_only_known_counts(self):
        telemetry = Telemetry.__new__(Telemetry)
        telemetry.cfg = {"node_id": "perimetr"}
        telemetry.events = telemetry.dropped = 0
        metrics = telemetry.metrics({"resources": {"open_fds": 7, "fd_limit": 1024,
                                                   "open_handles": None, "restart_required": False}})
        self.assertIn('perimeter_ha_open_fds{node="perimetr"} 7', metrics)
        self.assertIn('perimeter_ha_fd_limit{node="perimetr"} 1024', metrics)
        self.assertNotIn("perimeter_ha_open_handles", metrics)

    @unittest.skipUnless(sys.platform == "linux", "requires Linux descriptor limits")
    def test_real_socket_exhaustion_is_detected_before_http_can_stall(self):
        code = '''
import json, resource, socket
from guardian.resources import ResourceGuard
resource.setrlimit(resource.RLIMIT_NOFILE, (128, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
guard = ResourceGuard()
sockets = []
try:
    while not guard.sample()['restart_required']:
        sockets.append(socket.socket())
    early = guard.sample()
    while True:
        try:
            sockets.append(socket.socket())
        except OSError:
            break
    exhausted = guard.sample()
finally:
    for item in sockets:
        item.close()
recovered = guard.sample()
print(json.dumps(dict(early=early, exhausted=exhausted, recovered=recovered)))
'''
        result = subprocess.run([sys.executable, "-c", code],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["early"]["restart_required"])
        self.assertLess(report["early"]["open_fds"], 128)
        self.assertTrue(report["exhausted"]["restart_required"])
        self.assertFalse(report["recovered"]["restart_required"])
