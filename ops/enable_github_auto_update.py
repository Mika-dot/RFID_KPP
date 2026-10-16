"""One-time opt-in for existing Perimeter nodes; no code reinstall.

Updates the existing private node.json atomically. This tool intentionally
does not stop, restart, demote, fence, or change any SQL/worker process.
The running Guardian reads node.json only on its next *controlled* restart.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--enable", action="store_true")
    cli.add_argument("--expected-release", required=True)
    cli.add_argument("--config", type=Path,
                     default=Path("D:/PerimeterHA/node.json" if os.name == "nt"
                                  else "/etc/perimeter/node.json"))
    args = cli.parse_args()

    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_release):
        raise RuntimeError("ExactExistingReleaseRequired")
    if (os.name == "nt" and not __import__("ctypes").windll.shell32.IsUserAnAdmin()
            or os.name != "nt" and os.geteuid() != 0):
        raise RuntimeError("AdministratorRequired")

    config = args.config.resolve()
    cfg = json.loads(config.read_text(encoding="utf-8-sig"))
    if (os.name == "nt") != (cfg.get("node_id") == "physical"):
        raise RuntimeError("WrongOperatingSystemNode")
    state_dir = Path(cfg["state_dir"])
    release = json.loads((state_dir / "release.json").read_text(encoding="utf-8-sig"))
    current = release["current"]["sha"]

    if current != args.expected_release:
        raise RuntimeError("UnexpectedInstalledRelease")
    if release.get("pending"):
        raise RuntimeError("PendingTrialMustFinishFirst")
    if release.get("operator_pinned_release") != current:
        raise RuntimeError("ExpectedPinnedBaselineRequired")
    if (cfg.get("replication_enabled") is not True
            or cfg.get("fallback_enabled") is not True
            or cfg.get("auto_failback") is not False):
        raise RuntimeError("HAReplicationConfigurationNotAccepted")
    if not cfg.get("update_source") or not cfg.get("release_dir"):
        raise RuntimeError("ReleaseSourceNotConfigured")
    if (state_dir / "operator-release-install.json").exists():
        raise RuntimeError("DeploymentRecoveryJournalRequiresInspection")

    origin = subprocess.run(
        ["git", "-C", cfg["update_source"], "remote", "get-url", "origin"],
        check=True, text=True, capture_output=True, timeout=20,
    ).stdout.strip().removesuffix(".git")
    if origin != "https://github.com/Mika-dot/RFID_KPP":
        raise RuntimeError("UntrustedGitOrigin")

    print("NODE", cfg["node_id"])
    print("INSTALLED", current)
    print("AUTO_UPDATE_CONFIGURED", cfg.get("auto_update") is True)
    print("POLL_SECONDS", cfg.get("update_poll_sec", 60))
    print("GITHUB_TRACK", "origin/main")
    print("REQUIRES_GUARDIAN_RELOAD", cfg.get("auto_update") is not True)
    if not args.enable:
        print("READ_ONLY_NO_CHANGE")
        return 0

    # Snapshot the complete private JSON without printing any credentials.
    # Preserve private file permissions and owner using installed helper.
    sys.path.insert(0, release["current"]["root"])
    from deploy.ha.release_update import write_private
    new_cfg = dict(cfg)
    new_cfg["auto_update"] = True
    new_cfg["update_poll_sec"] = 60
    if new_cfg != cfg:
        write_private(config, new_cfg, config)
        print("CONFIG_ENABLED_GUARDIAN_RELOAD_REQUIRED")
    else:
        print("CONFIG_ALREADY_ENABLED")
    # "enabled" is the disk state only; the running Guardian may still have
    # its previous in-memory config until it is restarted.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("AUTO_UPDATE_SETUP_FAILED", type(exc).__name__,
              str(exc) if type(exc) in (RuntimeError, ValueError) else "", file=sys.stderr)
        raise SystemExit(2)
