from __future__ import annotations

import json
import os
from pathlib import Path

SERVICES = {
    "RfidReader": (18101, "deploy/monitored_rfid_recovery_v2.py"),
    "RusGuardSync": (18102, "deploy/monitored_rusguard.py"),
    "Yolo": (18103, "deploy/monitored_yolo.py"),
    "Aggregator": (18104, "deploy/monitored_aggregator.py"),
    "WebDashboard": (18105, "web/kpp_reel_dashboard_v3_fixed.py"),
}


def read_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    required = ("node_id", "root", "state_dir", "python", "nodes")
    for name in required:
        if not cfg.get(name):
            raise ValueError("Missing configuration: " + name)
    ids = [n["id"] for n in cfg["nodes"]]
    if len(ids) != 3 or len(set(ids)) != 3 or cfg["node_id"] not in ids:
        raise ValueError("Exactly three unique nodes, including this node, are required")
    if sorted(n["priority"] for n in cfg["nodes"]) != [1, 2, 3]:
        raise ValueError("Node priorities must be 1, 2, 3")
    cfg["root"] = str(Path(cfg["root"]).resolve())
    cfg["state_dir"] = str(Path(cfg["state_dir"]).resolve())
    token = os.environ.get("PERIMETER_HA_TOKEN", "")
    if len(token) < 32 or token.startswith("CHANGE_"):
        raise ValueError("A real PERIMETER_HA_TOKEN of at least 32 characters is required")
    if not os.environ.get("PERIMETER_HA_SQL"):
        raise ValueError("PERIMETER_HA_SQL is required")
    return cfg


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
