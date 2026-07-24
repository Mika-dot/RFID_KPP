from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)

SERVICES = (
    ("deploy/RUN_RFID_READER_V3.cmd", "RFID_reader_v4"),
    ("deploy/RUN_RUSGUARD_V3.cmd", "DB_RusGard"),
    ("deploy/RUN_RTSP_V3.cmd", "RTSP"),
    ("deploy/RUN_AGGREGATOR_V3.cmd", "KPP"),
    ("deploy/RUN_WEB_V3.cmd", "web"),
)


def build_command(comspec: str, wrapper: Path) -> list[str]:
    # No positional arguments are passed into the wrapper. This avoids the
    # Windows START/.cmd quoting bug that produced empty %1/%2/%3 values.
    return [comspec, "/D", "/K", "call", str(wrapper)]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    comspec = os.environ.get("COMSPEC") or "cmd.exe"

    missing: list[str] = []
    for wrapper_rel, work_rel in SERVICES:
        wrapper = root / wrapper_rel
        work_dir = root / work_rel
        if not wrapper.is_file():
            missing.append(str(wrapper))
        if not work_dir.is_dir():
            missing.append(str(work_dir))

    if missing:
        print("[FATAL] Startup files/directories are missing:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        return 2

    started: list[subprocess.Popen[bytes]] = []
    try:
        for wrapper_rel, work_rel in SERVICES:
            wrapper = (root / wrapper_rel).resolve()
            work_dir = (root / work_rel).resolve()
            proc = subprocess.Popen(
                build_command(comspec, wrapper),
                cwd=str(work_dir),
                env=os.environ.copy(),
                creationflags=CREATE_NEW_CONSOLE,
            )
            started.append(proc)
            print(f"[OK] Started {wrapper.name}, pid={proc.pid}")
    except Exception as exc:
        print(f"[FATAL] Cannot launch service consoles: {exc}", file=sys.stderr)
        for proc in started:
            try:
                proc.terminate()
            except Exception:
                pass
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
