"""Bounded, non-redirecting HTTP for control and peer credentials."""
from __future__ import annotations
import json
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def json_request(url, token=None, body=None, timeout=3, max_bytes=256*1024, parse_json=True):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers,
        data=json.dumps(body, ensure_ascii=False, allow_nan=False,separators=(",",":")).encode("utf-8") if body is not None else None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(max_bytes+1)
        if len(raw) > max_bytes:
            raise ValueError("OversizedResponse")
        return response.status, json.loads(raw) if parse_json else None
