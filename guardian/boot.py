"""Stable supervisor entry point, selecting the durable activated release."""
import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    record = Path(cfg["state_dir"]) / "release.json"
    current = json.loads(record.read_text())["current"] if record.exists() else {"root":cfg["root"]}
    root = current["root"]
    return subprocess.call([current.get("python", cfg["python"]), "-m", "guardian", "--config", args.config, "serve"],
                           cwd=root, env=os.environ.copy())


if __name__ == "__main__":
    raise SystemExit(main())
