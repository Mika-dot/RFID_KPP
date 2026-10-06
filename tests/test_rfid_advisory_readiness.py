"""A partial activity warning must not restart HA or hide real RFID faults."""
import tempfile
import unittest
from pathlib import Path

from common.observability import HealthReporter


class RfidAdvisoryReadinessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.required = ("business_flow", "database", "delivery_writer", "local_spool",
                         "reader_loop", "rfid_reader", "rfid_tcp")
        self.reporter = HealthReporter("Perimeter.RfidReader", "test", self.required,
                                       Path(self.folder.name) / "heartbeat.json")
        for name in self.required:
            self.reporter.set_dependency(name, "ok")
        self.reporter.set_metric("business_flow_latched", False)
        self.reporter.set_metric("video_recent_events", 1)
        self.reporter.set_dependency("business_flow", "degraded",
            detail="rfid_stale_with_partial_activity_evidence", data_age_seconds=5260.4)

    def test_actual_partial_evidence_keeps_operational_readiness_and_visible_business_warning(self):
        payload, code = self.reporter.snapshot()
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["dependencies"]["business_flow"]["status"], "degraded")
        self.assertEqual(payload["dependencies"]["business_flow"]["data_age_seconds"], 5260.4)
        self.assertEqual(payload["warnings"]["business_flow"]["detail"],
                         "rfid_stale_with_partial_activity_evidence")
        self.assertFalse(payload["metrics"]["business_flow_latched"])

    def test_every_transport_storage_or_delivery_failure_still_fails_readiness(self):
        for name in self.required[1:]:
            with self.subTest(name=name):
                self.reporter.set_dependency(name, "unavailable", detail="connection_failure")
                self.assertEqual(self.reporter.snapshot()[1], 503)
                self.reporter.set_dependency(name, "ok")

    def test_confirmed_activity_and_latched_fault_never_become_advisory(self):
        for detail in ("rfid_stale_while_video_active", "rfid_stale_while_video_and_warehouse_active",
                       "rfid_business_flow_fault_latched", "state_file_invalid"):
            with self.subTest(detail=detail):
                self.reporter.set_dependency("business_flow", "unavailable", detail=detail)
                payload, code = self.reporter.snapshot()
                self.assertEqual(code, 503)
                self.assertNotIn("warnings", payload)

    def test_missing_or_true_latch_metric_fails_closed_even_with_partial_reason(self):
        for value in (None, True, "false", 0):
            self.reporter.set_metric("business_flow_latched", value)
            self.assertEqual(self.reporter.snapshot()[1], 503)

    def test_stale_warning_probe_is_a_failure(self):
        self.reporter.set_dependency("business_flow", "degraded",
            detail="rfid_stale_with_partial_activity_evidence", stale_after_seconds=30)
        self.reporter.dependencies["business_flow"]["_updated_mono"] -= 100
        payload, code = self.reporter.snapshot()
        self.assertEqual(code, 503)
        self.assertEqual(payload["dependencies"]["business_flow"]["detail"], "stale")

    def test_unknown_degradation_and_wrong_service_do_not_get_exception(self):
        self.reporter.set_dependency("business_flow", "degraded", detail="unknown_reason")
        self.assertEqual(self.reporter.snapshot()[1], 503)
        self.reporter.set_dependency("business_flow", "degraded",
            detail="rfid_stale_with_partial_activity_evidence")
        self.reporter.service = "Perimeter.Aggregator"
        self.assertEqual(self.reporter.snapshot()[1], 503)

    def test_fatal_reader_never_gets_ready_and_fresh_data_removes_warning(self):
        self.reporter.fatal = True
        self.assertEqual(self.reporter.snapshot()[1], 503)
        self.reporter.fatal = False
        self.reporter.set_dependency("business_flow", "ok", detail="rfid_source_fresh")
        payload, code = self.reporter.snapshot()
        self.assertEqual(code, 200)
        self.assertNotIn("warnings", payload)


if __name__ == "__main__":
    unittest.main()
