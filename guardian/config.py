from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

SERVICES = {
    "RfidReader": (18101, "deploy/monitored_rfid_recovery_v2.py"),
    "RusGuardSync": (18102, "deploy/monitored_rusguard.py"),
    "Yolo": (18103, "deploy/monitored_yolo.py"),
    "Aggregator": (18104, "deploy/monitored_aggregator.py"),
    "WebDashboard": (18105, "web/kpp_reel_dashboard_v3_fixed.py"),
}

_JSON_REPLACE_LOCK = threading.Lock()


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
    if type(cfg.get("replication_enabled", False)) is not bool:
        raise ValueError("replication_enabled must be boolean")
    if cfg.get("replication_enabled", False):
        from common.replicated_ingest import validate_peers
        validate_peers(cfg["nodes"], cfg["node_id"])
    if type(cfg.get("hardware_fencing_required", False)) is not bool:
        raise ValueError("hardware_fencing_required must be boolean")
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
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as f:
            tmp = Path(f.name)
            json.dump(value, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        # Windows can reject concurrent replacements or a short-lived reader's
        # handle. Keep the old complete record and retry only sharing/access errors.
        with _JSON_REPLACE_LOCK:
            for attempt in range(10):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError as exc:
                    if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 9:
                        raise
                    time.sleep(.05)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
