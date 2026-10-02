"""Prepare or run the physical node from a private JSON bundle, without CMD secrets."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from environment_tool import (
    ALIASES, PREFIXES, TransferError, atomic_private_json, missing_fields,
    normalized_environment, private_directory,
)

SOURCE = Path(__file__).resolve().parents[2]


def windows_environment(bundle):
    if (not isinstance(bundle, dict) or bundle.get("version") != 1
            or not re.fullmatch(r"[0-9a-f]{64}", str(bundle.get("ha_token", "")))):
        raise TransferError("Invalid transfer bundle")
    source = bundle.get("environment")
    if not isinstance(source, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and "\0" not in v
        for k, v in source.items()
    ):
        raise TransferError("Invalid environment in bundle")
    env = normalized_environment(source)
    missing = missing_fields(env)
    missing += [k for k in ("KPP_WEB_AUTH_USER", "KPP_WEB_AUTH_PASSWORD") if not env.get(k)]
    if missing:
        raise TransferError("Missing settings: " + ", ".join(missing))
    env.update(PERIMETER_HA_TOKEN=bundle["ha_token"], PERIMETER_HA_SQL=env["KPP_CONN_STR"],
               KPP_WEB_AUTH_REQUIRED="1")
    return env


def read_bundle(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def prepare(config, bundle, production_root):
    env = windows_environment(read_bundle(bundle))
    config = Path(config).absolute()
    production_root = Path(production_root).absolute()
    if config.exists():
        raise TransferError("Windows node config already exists; it was preserved")
    if SOURCE == production_root or not (SOURCE / "guardian/boot.py").is_file():
        raise TransferError("Run preparation from a separate HA source checkout")
    node = json.loads((SOURCE / "deploy/ha/windows.example.json").read_text(encoding="utf-8-sig"))
    node.update(root=str(SOURCE), update_source=str(SOURCE),
                state_dir=str(config.parent / "state"), release_dir=str(config.parent / "releases"),
                python=str(production_root / "venv64/Scripts/python.exe"),
                python32=str(production_root / "RFID_readers/Версия (БД)/venv310_32/Scripts/python.exe"))
    if not all(Path(node[k]).is_file() for k in ("python", "python32")):
        raise TransferError("Verify the physical node's 64-bit and 32-bit Python paths")
    urls = {"physical": "http://172.31.0.188:18200", "perimetr": "http://172.31.0.134:18200",
            "comparator": "http://172.31.0.192:18200"}
    for peer in node["nodes"]:
        peer["url"] = urls[peer["id"]]
    private_directory(config.parent)
    for key in ("state_dir", "release_dir"):
        Path(node[key]).mkdir(parents=True, exist_ok=True)
    atomic_private_json(config, node)
    return {"prepared": True, "node_id": "physical", "source": str(SOURCE),
            "web_auth_present": bool(env["KPP_WEB_AUTH_PASSWORD"]), "service_started": False}


def launch(config, bundle, command):
    node = json.loads(Path(config).read_text(encoding="utf-8-sig"))
    if node.get("node_id") != "physical" or node.get("controller_enabled") is not False:
        raise TransferError("The physical node must not run a controller")
    env = {k: v for k, v in os.environ.items()
           if not (k.upper().startswith(PREFIXES) or k.upper() in ALIASES)}
    env.update(windows_environment(read_bundle(bundle)))
    if command == "serve":
        argv = [node["python"], str(SOURCE / "guardian/boot.py"), "--config", str(config)]
    else:
        argv = [node["python"], "-m", "guardian", "--config", str(config), "doctor"]
    return subprocess.call(argv, cwd=node["root"], env=env, shell=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "doctor", "serve"))
    parser.add_argument("--config", type=Path, default=Path("D:/PerimeterHA/node.json"))
    parser.add_argument("--bundle", type=Path,
                        default=Path("D:/PerimeterHA/transfer-private/environment.local.json"))
    parser.add_argument("--production-root", type=Path, default=Path("D:/Desktop/RFID_KPP-main"))
    args = parser.parse_args(argv)
    try:
        if os.name != "nt":
            raise TransferError("Physical node preparation requires native Windows")
        if args.command == "prepare":
            print(json.dumps(prepare(args.config, args.bundle, args.production_root), indent=2))
            return 0
        return launch(args.config, args.bundle, args.command)
    except Exception as exc:
        error = str(exc) if isinstance(exc, TransferError) else type(exc).__name__
        print(json.dumps({"error": error}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
