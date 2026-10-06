"""Regressions from initial HA activation: pooled immutable keys and cp1251 pipes."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from guardian.processes import Processes

ROOT = Path(__file__).resolve().parents[1]


def load_fencing():
    spec = importlib.util.spec_from_file_location("fencing_under_test", ROOT / "guardian/fencing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerFencingTests(unittest.TestCase):
    def driver(self, lease=True, source=False):
        connection = Mock()
        def execute(sql, *params):
            cursor = Mock()
            cursor.fetchone.return_value = (("RusGuardDB" if source else "Perimeter",)
                                           if sql == "SELECT DB_NAME()" else (1,) if lease else None)
            return cursor
        connection.execute.side_effect = execute
        driver = types.SimpleNamespace(pooling=True)
        def original(*args, **kwargs):
            self.assertFalse(driver.pooling, "Pool must be disabled before the first connect")
            return connection
        driver.connect = original
        return driver, connection

    def install(self, driver):
        module = load_fencing()
        with patch.dict(sys.modules, {"pyodbc": driver}), patch.dict(os.environ, {
            "PERIMETER_HA_NODE": "physical", "PERIMETER_HA_EPOCH": "6",
            "SRC_DATABASE": "RusGuardDB"
        }, clear=True):
            module.install()
        return module

    def test_worker_disables_pool_before_connect_and_retains_immutable_identity(self):
        driver, connection = self.driver()
        self.install(driver)
        with patch.dict(os.environ, {"SRC_DATABASE": "RusGuardDB"}, clear=True):
            self.assertIs(driver.connect("private connection"), connection)
        identity = [call for call in connection.execute.call_args_list if "sp_set_session_context" in call.args[0]]
        self.assertEqual([call.args[1] for call in identity], ["physical", 6])
        self.assertTrue(all("@read_only=1" in call.args[0] for call in identity))
        connection.commit.assert_called_once()

    def test_stale_epoch_is_rejected_and_connection_closed(self):
        driver, connection = self.driver(lease=False)
        self.install(driver)
        with self.assertRaisesRegex(RuntimeError, "WriterLeaseInvalid"):
            driver.connect("private connection")
        connection.close.assert_called_once()
        connection.commit.assert_not_called()

    def test_source_database_stays_read_only_exception_to_output_lease_check(self):
        driver, connection = self.driver(source=True)
        self.install(driver)
        with patch.dict(os.environ, {"SRC_DATABASE": "RusGuardDB"}, clear=True):
            driver.connect("private source")
        self.assertFalse(any("KPP_HA_Lease" in call.args[0] for call in connection.execute.call_args_list))

    def test_non_ha_process_is_unchanged(self):
        driver, _ = self.driver()
        original = driver.connect
        with patch.dict(sys.modules, {"pyodbc": driver}), patch.dict(os.environ, {}, clear=True):
            load_fencing().install()
        self.assertTrue(driver.pooling)
        self.assertIs(driver.connect, original)


class WorkerEncodingTests(unittest.TestCase):
    def test_actual_worker_pipe_overrides_cp1251_and_preserves_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy").mkdir()
            (root / "deploy/run_service.py").write_text(
                "print('\\u2588\\U0001f310 КПП', flush=True)\n", encoding="utf-8")
            manager = Processes({"root": str(root), "state_dir": str(root / "state"),
                                 "node_id": "physical", "python": sys.executable,
                                 "env": {"PYTHONIOENCODING": "cp1251", "PYTHONUTF8": "0"}})
            # The real child and pipe exercise encoding; PID metadata is unrelated.
            metadata = types.SimpleNamespace(Process=lambda pid: types.SimpleNamespace(create_time=lambda: 1.0))
            with patch("guardian.processes.SERVICES", {"WebDashboard": (18105, "unused.py")}), \
                    patch.dict(sys.modules, {"psutil": metadata}):
                try:
                    manager.start(6)
                    self.assertEqual(manager.children["WebDashboard"].wait(timeout=10), 0)
                    manager.logs["WebDashboard"].join(timeout=5)
                    log = root / "state/logs/WebDashboard.log"
                    self.assertEqual(log.read_text(encoding="utf-8").strip(), "█🌐 КПП")
                finally:
                    manager.children.clear()  # Child has already been waited/reaped.
                    manager.stop()


if __name__ == "__main__":
    unittest.main()
