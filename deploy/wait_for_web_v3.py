from __future__ import annotations

import argparse
import time
import urllib.error
import urllib.request
import webbrowser


def wait_for_url(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                if 200 <= int(response.status) < 500:
                    return True
        except (OSError, urllib.error.URLError, TimeoutError):
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args()

    if not wait_for_url(args.url, args.timeout):
        print(f"[WARN] URL did not become ready: {args.url}")
        return 1

    print(f"[OK] Web is ready: {args.url}")
    if args.open_browser:
        webbrowser.open(args.url, new=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
