"""Retry exact physical deployment only after ordinary HA admission succeeds.

This operator helper is deliberately NOT part of the qualified production release.
It never writes SQL state, disables fencing, or mutates the installed Git checkout.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

RELEASE = "06e32f72b88834fc113c21a637599cad12ce7981"
OLD = "62d0caa2863765419f98251372d7d376944fe004"
CONFIG = Path("D:/PerimeterHA/node.json")
ENVIRONMENT = Path("D:/PerimeterHA/transfer-private/environment.local.json")
RETRYABLE_APPLY = ("HealthySingleOwnerRequired", "PreparedDeploymentInputsChanged",
                   "QualifiedReserveRequired:", "SecondReplicaReceiverRequiredUseHeldReserveStage")


def output(*parts):
    print(*parts, flush=True)


def file_record(cfg):
    return json.loads((Path(cfg["state_dir"]) / "release.json").read_text(encoding="utf-8-sig"))


def execute(argv):
    output("RUN", " ".join(str(arg) for arg in argv[1:6]))
    process = subprocess.Popen([str(a) for a in argv], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", bufsize=1)
    lines = []
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip("\r\n")
        output(line)
        lines.append(line)
        if len(lines) > 30:
            del lines[:-30]
    return process.wait(), "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if os.name != "nt" or not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("AdministratorPowerShellRequired")
    if args.timeout < 30 or args.timeout > 3600:
        raise ValueError("InvalidTimeout")

    cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    if cfg.get("node_id") != "physical":
        raise RuntimeError("PhysicalNodeRequired")
    state = Path(cfg["state_dir"])
    journal = state / "operator-release-install.json"
    record = file_record(cfg)
    current = record["current"]["sha"]
    if current == RELEASE:
        output("ALREADY_INSTALLED", RELEASE)
        return 0
    if current != OLD:
        raise RuntimeError("UnexpectedInstalledRelease:" + current)
    root = Path(record["current"]["root"])
    updater = root / "deploy" / "ha" / "release_update.py"
    if not updater.is_file():
        raise RuntimeError("InstalledUpdaterMissing")

    sys.path[:0] = [str(root), str(root / "deploy" / "ha")]
    from deploy.ha import release_update as u
    environment = u.load_environment(SimpleNamespace(environment=ENVIRONMENT), cfg)
    token = environment["PERIMETER_HA_TOKEN"]
    python = str(cfg["python"])

    prep = [python, "-B", str(updater), "--prepare", "--replace-pinned-trial", "--release", RELEASE]
    apply = [python, "-B", str(updater), "--apply", "--handoff", "--pin-release",
             "--refresh-bootstrap", "--release", RELEASE]
    deadline = time.monotonic() + args.timeout
    last_report = 0.0
    attempts = 0

    while time.monotonic() < deadline:
        record = file_record(cfg)
        if record["current"]["sha"] == RELEASE:
            output("PHYSICAL_RELEASE_INSTALLED", RELEASE)
            return 0
        if record["current"]["sha"] != OLD:
            raise RuntimeError("InstalledReleaseChangedExternally")
        if journal.exists():
            raise RuntimeError("STOP: InstallRecoveryJournalExists; no retry")

        try:
            rows, owner = u.inventory(cfg, token, allow_degraded=True)
            for name in ("perimetr", "comparator"):
                s = rows[name]
                if (s.get("release_sha") != RELEASE or s.get("prepared") is not True
                        or s.get("faulted") is not False
                        or s.get("operator_maintenance") is not False
                        or s.get("replication_enabled") is not True
                        or s.get("fallback_enabled") is not True):
                    raise RuntimeError("ReserveNotReady:" + name)
            healthy = (owner is not None and rows[owner].get("healthy") is True
                       and rows[owner].get("prepared") is True
                       and rows[owner].get("faulted") is False)
            if not healthy:
                raise RuntimeError("NoHealthyOwner")
            u.require_reserves(rows, "physical", RELEASE)
        except Exception as exc:
            if time.monotonic() - last_report >= 10:
                output("WAIT_HA", type(exc).__name__, str(exc)[:120])
                last_report = time.monotonic()
            time.sleep(2)
            continue

        output("HA_ADMISSION_OK", "owner=" + str(owner), "epoch=" + str(rows[owner]["epoch"]))
        # Validate the exact receipt AFTER a healthy observation; never rewrite a
        # qualified candidate or ledger manually.
        try:
            u.check_prepared(SimpleNamespace(release=RELEASE, config=CONFIG), cfg)
        except (RuntimeError, FileNotFoundError) as exc:
            if str(exc) != "PreparedDeploymentInputsChanged" and not isinstance(exc, FileNotFoundError):
                raise
            output("REFRESH_QUALIFICATION")
            rc, _ = execute(prep)
            if rc:
                raise RuntimeError("PrepareFailed: inspect output above")
            continue

        # Recheck the admission gate immediately before the native updater.
        try:
            admitted, active = u.inventory(cfg, token)
            u.require_reserves(admitted, "physical", RELEASE)
            if active is None:
                raise RuntimeError("HealthySingleOwnerRequired")
        except RuntimeError as exc:
            output("HA_MOVED_BEFORE_APPLY", str(exc))
            time.sleep(1)
            continue

        attempts += 1
        output("APPLY_ATTEMPT", attempts)
        rc, log = execute(apply)
        record = file_record(cfg)
        if rc == 0 and record["current"]["sha"] == RELEASE:
            output("PHYSICAL_RELEASE_INSTALLED", RELEASE)
            output("GUARDIAN_RUNTIME_ACCEPTANCE_PENDING_INDEPENDENT_VERIFICATION")
            return 0
        if record["current"]["sha"] == RELEASE:
            raise RuntimeError("InstalledButApplyNotConfirmed; inspect runtime before retry")
        if journal.exists():
            raise RuntimeError("InstallRecoveryJournalExists; stop and inspect")
        if rc != 0 and any(reason in log for reason in RETRYABLE_APPLY):
            output("RETRY_AFTER_PREFLIGHT_RACE")
            time.sleep(2)
            continue
        raise RuntimeError("ApplyFailedNonRetryable; inspect output above")

    raise RuntimeError("TimedOutWithoutQualifiedHandoff; no unsafe changes made")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        output("ROLLOUT_STOPPED", type(exc).__name__, str(exc))
        raise SystemExit(2)
