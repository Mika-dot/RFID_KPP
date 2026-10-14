import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class FactoryTestDiscoveryTests(unittest.TestCase):
    def test_installed_tests_package_cannot_shadow_replica_fixture(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            foreign = Path(folder) / "tests"
            foreign.mkdir()
            (foreign / "__init__.py").write_text("", encoding="utf-8")
            result = subprocess.run([
                sys.executable, "-m", "unittest", "discover", "-s", "tests",
                "-p", "test_replica_recovery_cursors.py", "-v",
            ], cwd=root, env=dict(os.environ, PYTHONPATH=folder),
                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Ran 4 tests", result.stderr)
