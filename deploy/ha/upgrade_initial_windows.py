"""Update a never-started physical HA checkout while legacy services keep running."""
import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path


CACHES = frozenset((
    "common/__pycache__/__init__.cpython-310.pyc",
    "common/__pycache__/single_instance.cpython-310.pyc",
    "common/__pycache__/kpp_core_v3.cpython-310.pyc",
))


def names(raw):
    return [os.fsdecode(name) for name in raw.split(b"\0") if name]


def cache_plan(source, target, git):
    if git("diff", "--cached", "--name-only", "-z"):
        raise ValueError("Staged changes require review")
    if git("ls-files", "--others", "--exclude-standard", "-z"):
        raise ValueError("Untracked files require review")
    git("merge-base", "--is-ancestor", "HEAD", target)
    plan = []
    for name in names(git("diff", "--name-only", "-z")):
        if name not in CACHES:
            raise ValueError("Non-cache changes require review: " + name)
        path = source / name
        if not stat.S_ISREG(path.lstat().st_mode) or not path.resolve().is_relative_to(source):
            raise ValueError("Non-regular cache requires review: " + name)
        raw = git("diff", "--raw", "--", name).split(b"\t", 1)[0].split()
        if len(raw) != 5 or raw[0][1:] != raw[1] or raw[4] != b"M":
            raise ValueError("Cache mode/deletion requires review: " + name)
        if git("show", "HEAD:" + name) != git("show", target + ":" + name):
            raise ValueError("Target changes a modified cache: " + name)
        plan.append((name, path.read_bytes()))
    return plan


def save_caches(backup, plan):
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, data in plan:
        path = backup / "files" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if path.read_bytes() != data:
            raise ValueError("Cache backup verification failed")
    manifest = [{"path": name, "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in plan]
    (backup / "files.json").write_text(json.dumps(manifest), encoding="utf-8")


def restore_caches(source, git, plan):
    for name, data in plan:
        if (source / name).read_bytes() != data:
            raise ValueError("Cache changed after inspection: " + name)
    if plan:
        git("restore", "--source=HEAD", "--worktree", "--", *(name for name, _ in plan))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--from-release", default="d138fcc3d8d674833937e696d60a7fdab448c590")
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise ValueError("Run this initial physical upgrade on native Windows")
    if any(len(value) != 40 or any(c not in "0123456789abcdef" for c in value)
           for value in (args.release, args.from_release)):
        raise ValueError("Exact commit SHAs are required")
    # This utility runs outside the checkout and must not regenerate tracked caches.
    sys.dont_write_bytecode = True
    source = Path("D:/PerimeterHA/source").resolve()
    sys.path[:0] = [str(source), str(source / "deploy/ha")]
    from windows_tool import read_bundle, windows_environment
    from guardian.sql import SqlStore
    from guardian.probes import services_health
    import psutil

    config = Path("D:/PerimeterHA/node.json")
    cfg = json.loads(config.read_text(encoding="utf-8-sig"))
    if (cfg["node_id"] != "physical" or cfg.get("controller_enabled") is not False
            or Path(cfg["root"]).resolve() != source):
        raise ValueError("Unexpected physical node identity or HA source")
    record = Path(cfg["state_dir"]) / "release.json"
    if record.exists():
        raise ValueError("This utility requires a never-started physical Guardian")
    os.environ.update(windows_environment(read_bundle(
        "D:/PerimeterHA/transfer-private/environment.local.json")))
    git_exe = "C:/Program Files/Git/cmd/git.exe"

    def git(*arguments):
        return subprocess.check_output([git_exe, "-C", str(source), *arguments], timeout=90)

    def precheck():
        state = subprocess.check_output([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "(Get-ScheduledTask -TaskName 'PerimeterGuardian' -ErrorAction Stop).State"
        ], timeout=20).decode().strip()
        if state != "Disabled" or record.exists():
            raise ValueError("Physical Guardian must remain disabled and never started")
        # Reject a manual launch even if its scheduled task is disabled.
        for process in psutil.process_iter(["name", "cmdline"]):
            if not (process.info["name"] or "").lower().startswith("python"):
                continue
            command = process.info["cmdline"]
            if command is None:
                raise ValueError("A Python process cannot be inspected")
            if any(str(config).lower() == arg.replace("/", "\\").lower() for arg in command):
                raise ValueError("A physical Guardian process is already running")
        lease = SqlStore().lease()
        if lease["enabled"] or lease["owner"] is not None:
            raise ValueError("HA must remain disabled throughout this initial upgrade")
        health = services_health()
        if len(health) != 5 or not all(item["ok"] for item in health.values()):
            raise ValueError("Legacy services are not all healthy")
        return {"ha_enabled": lease["enabled"], "legacy_services": {k: v["ok"] for k, v in health.items()}}

    print("PHYSICAL_UPGRADE_PRECHECK", json.dumps(precheck()), flush=True)
    if git("rev-parse", "HEAD").decode().strip() != args.from_release:
        raise ValueError("Unexpected initial physical checkout SHA")
    origin = git("remote", "get-url", "origin").decode().strip().removesuffix(".git")
    if origin != "https://github.com/Mika-dot/RFID_KPP":
        raise ValueError("Unexpected source repository")
    git("fetch", "origin", args.release)
    plan = cache_plan(source, args.release, git)
    backup = Path(cfg["state_dir"]) / "upgrade-backups" / uuid.uuid4().hex
    save_caches(backup, plan)
    shutil.copy2(config, backup / "node.json")
    print("CACHE_BACKUP", str(backup), "files=" + str(len(plan)), flush=True)
    precheck()
    restore_caches(source, git, plan)
    git("merge", "--ff-only", args.release)
    if git("rev-parse", "HEAD").decode().strip() != args.release or git("status", "--porcelain"):
        raise ValueError("Checkout is not clean at the requested release")
    print("PHYSICAL_UPGRADE_POSTCHECK", json.dumps(precheck()), flush=True)
    print("PHYSICAL_SOURCE_UPDATED", args.release, flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("PHYSICAL_UPGRADE_FAILED", str(exc) if isinstance(exc, ValueError)
              else type(exc).__name__, file=sys.stderr, flush=True)
        raise SystemExit(2)
