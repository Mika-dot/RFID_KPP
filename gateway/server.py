from __future__ import annotations

import argparse
import base64
import hmac
import html
import http.client
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor

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

    def cached_events(self):
        def fetch(node):
            try:
                code, result = self.request(node["url"].rstrip("/") + "/events/recent", self.token, timeout=2)
                if (code == 200 and result.get("configured") is True and result.get("node") == node["id"]
                        and type(result.get("at")) in (int, float) and isinstance(result.get("events"), list)):
                    return result
            except Exception:
                pass
            return None
        with ThreadPoolExecutor(max_workers=3) as pool:
            available = [value for value in pool.map(fetch, self.nodes.values()) if value is not None]
        if not available:
            return {"source": "unavailable", "stale": True, "events": []}
        return max(available, key=lambda value: value["at"])


def validate_public_url(cfg):
    value = cfg.get("public_url")
    if not value:
        return
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("InvalidPublicWebAddress")
    executor_hosts = {urlsplit(node["url"]).hostname for node in cfg["nodes"] if node["id"] != "comparator"}
    if parsed.hostname in executor_hosts:
        raise ValueError("IndependentPublicWebAddressRequired")


def cached_page(data):
    lines = []
    for row in data.get("events", [])[:20]:
        if not isinstance(row, dict):
            continue
        cells = (row.get("FirstSeen"), row.get("SourceTag"), row.get("FinalDirection"),
                 "RFID" if row.get("RfidReadCount", 0) else "Склад", row.get("WarehouseDt"))
        lines.append("<tr>" + "".join("<td>" + html.escape(str(value or "—")) + "</td>" for value in cells) + "</tr>")
    status = "Устаревшие данные" if data.get("stale", True) else "Локальная копия"
    return ("<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<meta http-equiv='refresh' content='15'><title>Периметр • резервный просмотр</title>"
            "<style>body{background:#101822;color:#e6eef6;font:17px system-ui;padding:4vw}"
            "h1{font-size:28px}table{width:100%;border-collapse:collapse}td,th{padding:12px;text-align:left;border-bottom:1px solid #304454}"
            ".state{color:#f5bf58}td:nth-child(2){font:13px monospace;overflow-wrap:anywhere}</style>"
            "<h1>Периметр</h1><p class='state'>Основная БД или исполнитель недоступны • " + status + "</p>"
            "<p>Резервный просмотр. Данные не подтверждают текущую работу считывателя.</p>"
            "<table><thead><tr><th>Время</th><th>Метка</th><th>Направление</th><th>Источник</th><th>Склад</th></tr></thead><tbody>"
            + ("".join(lines) or "<tr><td colspan='5'>Локальная копия пока недоступна</td></tr>")
            + "</tbody></table></html>").encode("utf-8")


def create_server(router, host="127.0.0.1", port=5051, fallback_auth=None):
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

        def send_local(self, raw, mime="application/json", code=200):
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def fallback(self):
            if self.command not in {"GET", "HEAD"} or self.path not in {"/", "/fallback/events"} or not fallback_auth:
                return self.unavailable()
            expected = "Basic " + base64.b64encode((fallback_auth[0] + ":" + fallback_auth[1]).encode()).decode()
            if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="RFID KPP"')
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            data = router.cached_events()
            if self.path == "/fallback/events":
                return self.send_local(json.dumps(dict(data, read_only=True), ensure_ascii=False).encode())
            return self.send_local(cached_page(data), "text/html; charset=utf-8")

        def handle_proxy(self):
            # Never accept an absolute-form URL or Host-controlled upstream.
            if not self.path.startswith("/") or self.path.startswith("//") or "\r" in self.path or "\n" in self.path:
                return self.unavailable(400)
            if self.path in {"/health", "/health/live"} and self.command in {"GET", "HEAD"}:
                return self.send_local(b'{"status":"live"}')
            if not slots.acquire(blocking=False):
                return self.unavailable()
            conn = None
            try:
                try:
                    backend, identity = router.resolve()
                except Exception:
                    return self.fallback()
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
    validate_public_url(cfg)
    from guardian.sql import SqlStore
    credentials = (os.getenv("KPP_WEB_AUTH_USER"), os.getenv("KPP_WEB_AUTH_PASSWORD"))
    server = create_server(Router(cfg["nodes"], SqlStore(), os.environ["PERIMETER_HA_TOKEN"]),
                           cfg.get("listen", "0.0.0.0"), cfg.get("port", 5051),
                           fallback_auth=credentials if all(credentials) else None)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
