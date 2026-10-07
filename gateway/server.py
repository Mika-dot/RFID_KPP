from __future__ import annotations

import argparse
import http.client
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from guardian.net import json_request
from common.replicated_ingest import validate_peers

MAX_RESPONSE = 32 * 1024 * 1024
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}
REQUEST_HEADERS = {"authorization", "cookie", "content-type", "accept", "accept-language", "user-agent", "if-none-match", "if-modified-since", "range"}


class Router:
    def __init__(self, nodes, store, token, request=json_request):
        validate_peers(nodes)
        self.nodes, self.store, self.token, self.request = {n["id"]: n for n in nodes}, store, token, request
        for node in nodes:
            url = urlsplit(node["web_url"])
            if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                    or url.path not in {"", "/"} or url.query or url.fragment):
                raise ValueError("InvalidWebBackend")

    def resolve(self):
        lease = self.store.lease()
        if not lease.get("enabled") or not lease.get("valid") or lease.get("owner") not in self.nodes:
            raise RuntimeError("NoValidWebOwner")
        node = self.nodes[lease["owner"]]
        code, status = self.request(node["url"].rstrip("/")+"/status", self.token, timeout=2)
        if (code != 200 or status.get("node") != node["id"] or status.get("epoch") != lease["epoch"]
                or status.get("active") is not True or status.get("healthy") is not True
                or status.get("fencing_protocol") != 2 or status.get("operator_maintenance") is not False
                or type(status.get("sample_age")) not in (int, float) or not 0 <= status["sample_age"] <= 10):
            raise RuntimeError("WebOwnerNotReady")
        return node["web_url"], (lease["owner"], lease["epoch"])

    def unchanged(self, identity):
        lease = self.store.lease()
        return lease.get("enabled") and lease.get("valid") and (lease.get("owner"), lease.get("epoch")) == identity


def create_server(router, host="127.0.0.1", port=5051):
    slots = threading.BoundedSemaphore(16)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def unavailable(self, code=503):
            raw = b'{"error":"WebTemporarilyUnavailable"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Retry-After", "3")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def handle_proxy(self):
            # Never accept an absolute-form URL or Host-controlled upstream.
            if not self.path.startswith("/") or self.path.startswith("//") or "\r" in self.path or "\n" in self.path:
                return self.unavailable(400)
            if not slots.acquire(blocking=False):
                return self.unavailable()
            conn = None
            try:
                backend, identity = router.resolve()
                if self.path in {"/health", "/health/ready"}:
                    raw = b'{"status":"ok"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(raw)
                    return
                if self.headers.get("Transfer-Encoding"):
                    return self.unavailable(400)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 1024*1024:
                    return self.unavailable(413)
                body = self.rfile.read(size) if size else None
                if size and len(body) != size:
                    return self.unavailable(400)
                target = urlsplit(backend)
                cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
                conn = cls(target.hostname, target.port, timeout=10)
                headers = {k: v for k, v in self.headers.items() if k.lower() in REQUEST_HEADERS}
                # Browser authentication passes only to this configured Web backend;
                # the HA bearer token is used solely in Router.resolve().
                conn.request(self.command, self.path, body=body, headers=headers)
                response = conn.getresponse()
                raw = response.read(MAX_RESPONSE+1)
                if len(raw) > MAX_RESPONSE or not router.unchanged(identity):
                    return self.unavailable()
                self.send_response(response.status)
                connection_tokens = {x.strip().lower() for x in (response.getheader("Connection") or "").split(",")}
                for key, value in response.getheaders():
                    if key.lower() not in HOP_HEADERS | connection_tokens | {"content-length", "server", "date"}:
                        if key.lower() == "location" and value.startswith(backend.rstrip("/")+"/"):
                            value = value[len(backend.rstrip("/")):]
                        self.send_header(key, value)
                length = response.getheader("Content-Length", "0") if self.command == "HEAD" else str(len(raw))
                self.send_header("Content-Length", length)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(raw)
            except Exception:
                self.unavailable()
            finally:
                if conn:
                    conn.close()
                slots.release()

        do_GET = do_POST = do_HEAD = handle_proxy

        def log_message(self, *args):
            pass
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    from guardian.sql import SqlStore
    server = create_server(Router(cfg["nodes"], SqlStore(), os.environ["PERIMETER_HA_TOKEN"]),
                           cfg.get("listen", "0.0.0.0"), cfg.get("port", 5051))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
