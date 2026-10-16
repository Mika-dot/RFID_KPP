"""Safety regression for stale peer heartbeat vs fresh localhost /health/ready.

No listener, external network, database, or HA lease is accessed.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from common.observability import HealthReporter


class _Response:
    def __init__(self, service="Perimeter.RfidReader", status=200, state="ok"):
        self.status = status
        self.payload = json.dumps({"status": state, "service": service}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return self.payload


class _Opener:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def open(self, request, timeout=None):
        self.urls.append((request.full_url, timeout))
        return self.response


class PeerHeartbeatFallbackTests(unittest.TestCase):
    peer = "Perimeter.RfidReader"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.reporter = HealthReporter(
            service="Perimeter.Aggregator",
            version="test",
            required_dependencies=("rfid_reader",),
            heartbeat_path=directory / "Perimeter.Aggregator.json",
        )
        self.reporter.register_peer("rfid_reader", self.peer)
        self.path = directory / (self.peer + ".json")

    def heartbeat(self, status="degraded", age_seconds=0):
        t = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        self.path.write_text(json.dumps({"status": status, "heartbeat_at": t.isoformat()}),
                             encoding="utf-8")

    def snapshot(self):
        return self.reporter.snapshot(ready=True)

    def test_degraded_file_with_live_ready_peer_is_accepted(self):
        self.heartbeat(status="degraded")
        opener = _Opener(_Response())
        with patch("common.observability.build_opener", return_value=opener):
            data, code = self.snapshot()
        self.assertEqual(code, 200)
        self.assertEqual(data["dependencies"]["rfid_reader"]["status"], "ok")
        self.assertEqual(data["dependencies"]["rfid_reader"]["detail"], "live_health_confirmed")
        self.assertEqual(opener.urls[0][0], "http://127.0.0.1:18101/health/ready")
        self.assertLess(opener.urls[0][1], 1)

    def test_missing_or_stale_file_uses_verified_live_peer(self):
        for mode in ("missing", "stale"):
            with self.subTest(mode=mode):
                if mode == "missing":
                    self.path.unlink(missing_ok=True)
                else:
                    self.heartbeat(status="ok", age_seconds=180)
                with patch("common.observability.build_opener",
                           return_value=_Opener(_Response())):
                    data, code = self.snapshot()
                self.assertEqual(code, 200)
                self.assertEqual(data["dependencies"]["rfid_reader"]["status"], "ok")

    def test_http_503_stays_unavailable(self):
        self.heartbeat(status="degraded")
        with patch("common.observability.build_opener",
                   return_value=_Opener(_Response(status=503, state="degraded"))):
            data, code = self.snapshot()
        self.assertEqual(code, 503)
        self.assertEqual(data["dependencies"]["rfid_reader"]["status"], "unavailable")

    def test_wrong_service_identity_not_trusted(self):
        self.heartbeat(status="degraded")
        with patch("common.observability.build_opener",
                   return_value=_Opener(_Response(service="Perimeter.Yolo"))):
            data, code = self.snapshot()
        self.assertEqual(code, 503)
        self.assertEqual(data["dependencies"]["rfid_reader"]["status"], "unavailable")

    def test_live_connection_error_stays_unavailable(self):
        self.heartbeat(status="degraded")
        opener = _Opener(_Response())
        opener.open = lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionRefusedError())
        with patch("common.observability.build_opener", return_value=opener):
            data, code = self.snapshot()
        self.assertEqual(code, 503)
        self.assertEqual(data["dependencies"]["rfid_reader"]["status"], "unavailable")

    def test_valid_fresh_file_never_opens_network(self):
        self.heartbeat(status="ok")
        with patch("common.observability.build_opener",
                   side_effect=AssertionError("unexpected network request")):
            data, code = self.snapshot()
        self.assertEqual(code, 200)
        self.assertEqual(data["dependencies"]["rfid_reader"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
