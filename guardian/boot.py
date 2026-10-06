"""Stable supervisor entry point, selecting the durable activated release."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def rollback_failed_launch(cfg, record, launched):
    # Load helpers from the stable installation, not the failing release.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from common.single_instance import SingleInstanceError, SingleInstanceLock
    from guardian.config import atomic_json
    try:
        with SingleInstanceLock(str(Path(cfg["state_dir"]) / "guardian.lock")):
            if not record.exists():
                return False
            state = json.loads(record.read_text(encoding="utf-8"))
            if not state.get("pending") or not state.get("previous") or state["current"] != launched:
                return False
            rejected = set(state.get("rejected", []))
            rejected.add(launched["sha"])
            state.update(current=state["previous"], previous=None, pending=False,
                         trial_started=False, rejected=sorted(rejected))
            atomic_json(record, state)
            print(json.dumps({"event":"bootstrap_rollback", "sha":launched["sha"]}), flush=True)
            return True
    except SingleInstanceError:
        # A duplicate launcher must not roll back an agent that is still running.
        return False


def launch(cfg, config_path):
    record = Path(cfg["state_dir"]) / "release.json"
    current = (json.loads(record.read_text(encoding="utf-8"))["current"] if record.exists()
               else {"root":cfg["root"], "python":cfg["python"]})
    try:
        code = subprocess.call([current.get("python", cfg["python"]), "-m", "guardian",
                                "--config", str(config_path), "serve"],
                               cwd=current["root"], env=os.environ.copy())
    except OSError:
        code = 2
    # Restart requests and operator shutdown are not evidence of a defective release.
    if code not in (0, 75, -2, -15, 130, 143) and rollback_failed_launch(cfg, record, current):
        return 75
    return code


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    return launch(cfg, args.config)


if __name__ == "__main__":
    raise SystemExit(main())
