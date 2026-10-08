import base64
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock

from gateway.server import Router, cached_page, create_server, validate_public_url


class GatewayFallbackTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [dict(id=name, priority=index + 1, url="http://" + name + ":18200",
                           web_url="http://" + name + ":5050")
                      for index, name in enumerate(("physical", "perimetr", "comparator"))]
        store = Mock()
        store.lease.side_effect = TimeoutError("primary SQL unavailable")
        self.requests = []
        def request(url, token, timeout):
            self.requests.append((url, token))
            if "comparator" in url:
                return 200, dict(configured=True, node="comparator", at=100, stale=True, events=[
                    dict(EventId=1, FirstSeen="2026-10-08T12:00:00", SourceTag="<script>unsafe()</script>",
                         FinalDirection="OUT", RfidReadCount=0)])
            raise TimeoutError()
        self.router = Router(self.nodes, store, "ha-secret", request)
        self.server = create_server(self.router, "127.0.0.1", 0, ("operator", "local-test"))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(2)))
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def get(self, path, login=False, method="GET"):
        headers = {}
        if login:
            headers["Authorization"] = "Basic " + base64.b64encode(b"operator:local-test").decode()
        try:
            return urlopen(Request(self.url + path, headers=headers, method=method), timeout=3)
        except HTTPError as exc:
            return exc

    def test_gateway_liveness_survives_sql_failure_but_readiness_does_not(self):
        with self.get("/health") as response:
            self.assertEqual(200, response.code)
        with self.get("/health/ready") as response:
            self.assertEqual(503, response.code)

    def test_outage_view_requires_existing_browser_authentication(self):
        with self.get("/") as response:
            self.assertEqual(401, response.code)
            self.assertIn("Basic", response.headers["WWW-Authenticate"])
        self.assertFalse(self.requests)

    def test_authenticated_outage_page_is_read_only_escaped_and_marked_stale(self):
        with self.get("/", login=True) as response:
            self.assertEqual(200, response.code)
            raw = response.read().decode()
        self.assertIn("Устаревшие данные", raw)
        self.assertIn("&lt;script&gt;", raw)
        self.assertNotIn("<script>", raw)
        self.assertNotIn("ha-secret", raw)
        with self.get("/", login=True, method="POST") as response:
            self.assertEqual(503, response.code)

    def test_json_cache_preserves_source_and_read_only_marker(self):
        with self.get("/fallback/events", login=True) as response:
            data = json.load(response)
        self.assertTrue(data["read_only"])
        self.assertTrue(data["stale"])
        self.assertEqual("comparator", data["node"])
        self.assertTrue(all(url.endswith("/events/recent") and token == "ha-secret" for url, token in self.requests))

    def test_public_address_cannot_reuse_an_executor_ip(self):
        validate_public_url(dict(nodes=self.nodes, public_url="http://comparator:5051"))
        with self.assertRaisesRegex(ValueError, "Independent"):
            validate_public_url(dict(nodes=self.nodes, public_url="http://physical:5051"))


if __name__ == "__main__":
    unittest.main()
