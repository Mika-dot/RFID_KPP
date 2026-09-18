#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send a compact Sentry event after a Python/native service process exits."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability import SERVICE_CONFIG, flush_sentry, init_sentry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True, choices=sorted(SERVICE_CONFIG))
    parser.add_argument("--exit-code", required=True, type=int)
    args = parser.parse_args()

    if args.exit_code == 0:
        return 0

    release = os.getenv("PERIMETER_RELEASE", "3.4.7-resilience-audit")
    if init_sentry(args.service, release):
        try:
            import sentry_sdk

            with sentry_sdk.push_scope() as scope:
                scope.level = "fatal"
                scope.set_tag("service", args.service)
                scope.set_extra("exit_code", args.exit_code)
                sentry_sdk.capture_message(
                    f"{args.service} terminated with exit code {args.exit_code}",
                    level="fatal",
                )
            flush_sentry(3.0)
        except Exception:
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
