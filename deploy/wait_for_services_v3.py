#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wait until every production service reports functional readiness."""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


SERVICES = {
    "RfidReader": 18101,
    "RusGuardSync": 18102,
    "Yolo": 18103,
    "Aggregator": 18104,
    "WebDashboard": 18105,
}


def read_ready(port: int, request_timeout: float) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{port}/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=request_timeout) as response:
            raw = response.read(1024 * 1024)
            payload = json.loads(raw.decode("utf-8"))
            status = str(payload.get("status", "unknown"))
            return response.status == 200 and status == "ok", status
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(1024 * 1024).decode("utf-8"))
            return False, str(payload.get("status", f"http_{exc.code}"))
        except Exception:
            return False, f"http_{exc.code}"
    except Exception as exc:
        return False, type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--poll", type=float, default=2.0)
    parser.add_argument("--request-timeout", type=float, default=3.0)
    args = parser.parse_args()

    deadline = time.monotonic() + max(1.0, args.timeout)
    previous: dict[str, str] = {}
    latest: dict[str, str] = {}

    while time.monotonic() < deadline:
        all_ready = True
        latest = {}
        for name, port in SERVICES.items():
            ok, detail = read_ready(port, max(0.5, args.request_timeout))
            latest[name] = detail
            all_ready = all_ready and ok
            if previous.get(name) != detail:
                print(f"[READY] {name:14s} port={port} status={detail}")
        previous = latest
        if all_ready:
            print("[OK] All five Perimeter services are functionally ready.")
            return 0
        time.sleep(max(0.25, args.poll))

    print("[FATAL] Functional readiness timeout. Final states:")
    for name, port in SERVICES.items():
        print(f"  - {name:14s} port={port}: {latest.get(name, 'unknown')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
