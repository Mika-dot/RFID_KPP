from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.observability import HealthReporter, SERVICE_CONFIG
from deploy import run_service


ROOT = Path(__file__).resolve().parents[1]


class FakeReporter:
    def __init__(self) -> None:
        self.version = "test"
        self.required_dependencies = ()

    def register_probe(self, *args, **kwargs):
        return None

    def register_peer(self, *args, **kwargs):
        return None

    def set_dependency(self, *args, **kwargs):
        return None

    def start(self, *args, **kwargs):
        return None

    def stop(self):
        return None

    def mark_fatal(self, *args, **kwargs):
        return None


class ObservabilityRestoreTests(unittest.TestCase):
    def test_existing_health_port_contract_is_preserved(self) -> None:
        self.assertEqual(SERVICE_CONFIG["Perimeter.RfidReader"]["port"], 18101)
        self.assertEqual(SERVICE_CONFIG["Perimeter.RusGuardSync"]["port"], 18102)
        self.assertEqual(SERVICE_CONFIG["Perimeter.Yolo"]["port"], 18103)
        self.assertEqual(SERVICE_CONFIG["Perimeter.Aggregator"]["port"], 18104)
        self.assertEqual(SERVICE_CONFIG["Perimeter.WebDashboard"]["port"], 18105)

    def test_readiness_degrades_until_required_dependency_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reporter = HealthReporter(
                service="test",
                version="test",
                required_dependencies=("dependency",),
                heartbeat_path=Path(tmp) / "health.json",
                heartbeat_sec=10,
            )
            payload, code = reporter.snapshot(ready=True)
            self.assertEqual(code, 503)
            self.assertEqual(payload["status"], "degraded")

            liveness, live_code = reporter.snapshot(ready=False)
            self.assertEqual(live_code, 200)
            self.assertEqual(liveness["status"], "ok")

            reporter.touch_dependency("dependency")
            payload, code = reporter.snapshot(ready=True)
            self.assertEqual(code, 200)
            self.assertEqual(payload["status"], "ok")

    def test_run_service_preserves_normal_script_sibling_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "helper.py").write_text("VALUE = 123\n", encoding="utf-8")
            target = tmp_path / "target.py"
            target.write_text(
                "import helper\n"
                "assert helper.VALUE == 123\n",
                encoding="utf-8",
            )
            fake = FakeReporter()
            with mock.patch.object(run_service, "init_observability", return_value=fake), mock.patch.object(
                run_service, "_register_dependency_checks", return_value=None
            ), mock.patch.object(run_service, "flush_sentry", return_value=None):
                rc = run_service.main(
                    [
                        "--service",
                        "Perimeter.WebDashboard",
                        "--script",
                        str(target),
                    ]
                )
            self.assertEqual(rc, 0)

    def test_long_running_services_are_routed_through_monitored_adapters(self) -> None:
        expected = {
            "deploy/RUN_RFID_READER_V3.cmd": "deploy\\monitored_rfid_recovery.py",
            "deploy/RUN_RUSGUARD_V3.cmd": "deploy\\monitored_rusguard.py",
            "deploy/RUN_RTSP_V3.cmd": "deploy\\monitored_yolo.py",
            "deploy/RUN_AGGREGATOR_V3.cmd": "deploy\\monitored_aggregator.py",
        }
        for rel, needle in expected.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(needle, text, rel)


if __name__ == "__main__":
    unittest.main()
