"""Optional deterministic external fence; configured argv only, never LLM text."""
from __future__ import annotations
import json
import subprocess
import os


def fence_previous(cfg, node_id, epoch):
    required = cfg.get("hardware_fencing_required", False)
    if not required:
        return {"required": False, "verified": False}
    node = next(n for n in cfg["nodes"] if n["id"] == node_id)
    argv = node.get("fence_argv")
    if (not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv)
            or type(epoch) is not int):
        raise RuntimeError("HardwareFenceUnconfigured")
    env = dict(os.environ, PERIMETER_FENCE_NODE=node_id, PERIMETER_FENCE_EPOCH=str(epoch))
    result = subprocess.run(argv, env=env, shell=False, stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=min(8, cfg.get("hardware_fence_timeout_sec", 5)))
    if result.returncode or len(result.stdout) > 4096:
        raise RuntimeError("HardwareFenceFailed")
    try:
        receipt = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise RuntimeError("HardwareFenceReceiptInvalid") from None
    # External adapter must actually verify isolation. An exit code alone is not proof.
    if receipt != {"node": node_id, "isolated": True, "epoch": epoch}:
        raise RuntimeError("HardwareFenceReceiptInvalid")
    return {"required": True, "verified": True}


def allow_target(cfg, node_id, epoch):
    node = next(n for n in cfg["nodes"] if n["id"] == node_id)
    argv = node.get("unfence_argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
        raise RuntimeError("HardwareUnfenceUnconfigured")
    env = dict(os.environ, PERIMETER_FENCE_NODE=node_id, PERIMETER_FENCE_EPOCH=str(epoch))
    r = subprocess.run(argv, env=env, shell=False, stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    if r.returncode or len(r.stdout)>4096:
        raise RuntimeError("HardwareUnfenceFailed")
    if json.loads(r.stdout) != {"node":node_id, "reader_access":True, "epoch":epoch}:
        raise RuntimeError("HardwareUnfenceReceiptInvalid")
