from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import threading
import uuid
import time
from pathlib import Path

from guardian.config import atomic_json
from guardian.probes import preflight
from guardian.release_contract import MANIFEST, docs_only, raise_floor, validate_contract


class Updates:
    """Stage exact main SHA separately; never git pull into a running directory."""

    def __init__(self, cfg, store, telemetry):
        self.cfg, self.store, self.telemetry = cfg, store, telemetry
        self.path = Path(cfg["state_dir"]) / "release.json"
        self.lock = threading.RLock()
        self.main_sha = None
        self.staged = None
        self.quarantined = set()
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            self.cfg["root"] = self.state["current"]["root"]
            if self.state["current"].get("python"):
                self.cfg["python"] = self.state["current"]["python"]
            self.quarantined.update(self.state.get("rejected", []))
        else:
            try:
                sha = self.git("rev-parse", "HEAD", cwd=cfg["root"]).strip()
            except Exception:
                sha = cfg.get("release_sha", "manual")
            self.state = {"current": {"root": cfg["root"], "sha": sha, "python":cfg["python"]}, "previous": None, "pending": False}
            try:
                self.state["trusted_main_sha"] = self.git("rev-parse", "refs/remotes/origin/main").strip()
            except Exception:
                self.state["trusted_main_sha"] = None
            atomic_json(self.path, self.state)
        floor = raise_floor(self.state.get("runtime_contract_floor"))
        manifest = Path(self.cfg["root"]) / MANIFEST
        if not self.state.get("pending") and manifest.is_file():
            floor = raise_floor(floor, validate_contract(manifest.read_text(encoding="utf-8"), floor))
        self.state["runtime_contract_floor"] = floor
        atomic_json(self.path, self.state)

    def git(self, *args, cwd=None):
        result = subprocess.run(["git"]+list(args), cwd=cwd or self.cfg["update_source"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError("GitUpdateFailed")
        return result.stdout

    def stage(self):
        if not self.cfg.get("auto_update", True) or not self.cfg.get("update_source"):
            return
        # Main must advance from the deployed base; reject force-pushed unrelated history.
        self.git("fetch", "origin", "main")
        sha = self.git("rev-parse", "FETCH_HEAD").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Invalid main commit")
        with self.lock:
            current = self.state["current"]["sha"]
            if sha == current:
                self.main_sha = sha
                return
            if sha in self.quarantined or (self.staged and self.staged["sha"] == sha):
                return
        # Initial branch deployment may precede main; require the HA protocol to exist.
        self.git("cat-file", "-e", sha + ":guardian/__main__.py")
        if self.state.get("fencing_protocol_min", 0) >= 2:
            # SQL protocol 2 requires every future controller and rollback
            # candidate to use the same transaction barrier for epoch changes.
            tree = ast.parse(self.git("show", sha + ":guardian/sql.py"))
            constants = {}
            for statement in tree.body:
                if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant):
                    for target_name in statement.targets:
                        if isinstance(target_name, ast.Name):
                            constants[target_name.id] = statement.value.value
            if constants.get("FENCING_PROTOCOL", 0) != self.state["fencing_protocol_min"] or constants.get("EPOCH_BARRIER") != "Perimeter.HA.Epoch":
                raise RuntimeError("CandidateFencingProtocolIncompatible")
        # Follow main ancestry, not the deployed feature commit: a squash merge
        # legitimately creates a different commit than the initial installation.
        trusted = self.state.get("trusted_main_sha")
        if trusted:
            self.git("merge-base", "--is-ancestor", trusted, sha)
        else:
            self.git("merge-base", current, sha)
        try:
            manifest = self.git("show", sha + ":" + MANIFEST)
        except RuntimeError:
            self.reject_candidate(sha, "CandidateRuntimeContractMissing")
            raise RuntimeError("CandidateRuntimeContractMissing") from None
        try:
            validate_contract(manifest, self.state["runtime_contract_floor"])
        except RuntimeError as exc:
            self.reject_candidate(sha, str(exc))
            raise
        paths = self.git("diff", "--name-only", current, sha, "--").splitlines()
        if docs_only(paths):
            with self.lock:
                if self.state["current"]["sha"] != current:
                    raise RuntimeError("CandidateBaseChanged")
                self.staged = None
                # main_sha is the controller's runtime target, not merely the
                # newest remote commit. A docs commit must never trigger HA.
                self.main_sha = current
                self.state["trusted_main_sha"] = sha
                self.state["observed_main_sha"] = sha
                self.state["docs_only_sha"] = sha
                atomic_json(self.path, self.state)
            self.telemetry.event("update_docs_only", sha=sha)
            return
        target = Path(self.cfg["release_dir"]) / sha
        if not target.exists():
            self.git("worktree", "add", "--detach", str(target), sha)
        # Execute the accepted checker's file, not a candidate-owned test. It
        # protects the temporal RFID hotfix even if candidate tests are removed.
        checker = Path(__file__).with_name("release_contract.py").resolve()
        try:
            protected = subprocess.run([self.cfg["python"], str(checker), str(target)],
                cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=30)
        except subprocess.TimeoutExpired:
            self.reject_candidate(sha, "CandidateBusinessChecksTimeout")
            raise RuntimeError("CandidateBusinessChecksTimeout") from None
        if protected.returncode:
            self.reject_candidate(sha, "CandidateProtectedBusinessChecksFailed")
            raise RuntimeError("CandidateProtectedBusinessChecksFailed")
        # Do not run schema migrations here. Breaking changes are a distinct rollout.
        result = subprocess.run([self.cfg["python"], "-m", "unittest", "discover", "-s", "tests"],
            cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=240)
        if result.returncode:
            self.reject_candidate(sha, "CandidateTestsFailed")
            raise RuntimeError("CandidateTestsFailed")
        candidate_cfg = dict(self.cfg, root=str(target))
        report = preflight(candidate_cfg, self.store, active=True)
        if not report["ok"]:
            raise RuntimeError("CandidatePreflightFailed")
        with self.lock:
            if self.state["current"]["sha"] != current:
                raise RuntimeError("CandidateBaseChanged")
            self.main_sha = sha
            self.staged = {"root": str(target), "sha": sha, "python": self.cfg["python"]}
            self.state["trusted_main_sha"] = sha
            atomic_json(self.path, self.state)
        self.telemetry.event("update_staged", sha=sha)

    def reject_candidate(self, sha, reason):
        with self.lock:
            self.quarantined.add(sha)
            self.state["rejected"] = sorted(self.quarantined)
            if self.staged and self.staged["sha"] == sha:
                self.staged = None
            atomic_json(self.path, self.state)
        self.telemetry.event("update_candidate_rejected", sha=sha, reason=reason)

    def activate(self):
        with self.lock:
            if not self.staged:
                return False
            if self.state.get("pending"):
                return False
            self.state["previous"] = self.state["current"]
            self.state["current"] = self.staged
            self.state["pending"] = True
            self.state["trial_started"] = False
            self.state["activated_at"] = time.time()
            self.staged = None
            self.state["rejected"] = sorted(self.quarantined)
            atomic_json(self.path, self.state)
            self.cfg["root"] = self.state["current"]["root"]
            if self.state["current"].get("python"):
                self.cfg["python"] = self.state["current"]["python"]
            self.telemetry.event("update_activated", sha=self.state["current"]["sha"])
            return True

    def confirm(self):
        with self.lock:
            if self.state.get("pending"):
                manifest = Path(self.cfg["root"]) / MANIFEST
                if manifest.is_file():
                    value = validate_contract(manifest.read_text(encoding="utf-8"), self.state["runtime_contract_floor"])
                    self.state["runtime_contract_floor"] = raise_floor(self.state["runtime_contract_floor"], value)
                self.state["pending"] = False
                atomic_json(self.path, self.state)
                self.telemetry.event("update_confirmed", sha=self.state["current"]["sha"])

    def begin_trial(self):
        with self.lock:
            if self.state.get("pending"):
                self.state["trial_started"] = True
                atomic_json(self.path, self.state)

    def rollback(self):
        with self.lock:
            if not self.state.get("previous"):
                raise RuntimeError("NoPreviousRelease")
            rejected = self.state["current"]["sha"]
            self.quarantined.add(rejected)
            self.state["current"], self.state["previous"] = self.state["previous"], None
            self.state["pending"] = False
            self.state["trial_started"] = False
            self.state["rejected"] = sorted(self.quarantined)
            atomic_json(self.path, self.state)
            self.cfg["root"] = self.state["current"]["root"]
            if self.state["current"].get("python"):
                self.cfg["python"] = self.state["current"]["python"]
            self.main_sha = None
            self.telemetry.event("update_rejected", sha=rejected)

    def restore_release(self):
        current = dict(self.state["current"])
        if not re.fullmatch(r"[0-9a-f]{40}", current["sha"]):
            raise RuntimeError("NoExactReleaseSha")
        target = Path(self.cfg["release_dir"]) / (current["sha"] + "-repair-" + uuid.uuid4().hex[:8])
        self.git("worktree", "add", "--detach", str(target), current["sha"])
        self.staged = dict(current, root=str(target))
        # Explicit repair may replace an already pending, broken candidate.
        self.state["pending"] = False
        self.activate()

    def repair_dependencies(self):
        old = dict(self.state["current"])
        old.setdefault("python", self.cfg["python"])
        target = Path(self.cfg["state_dir"]) / "venvs" / uuid.uuid4().hex
        subprocess.run([self.cfg["python"], "-m", "venv", str(target)], check=True, timeout=90,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        device = self.cfg.get("env", {}).get("RFID_YOLO_DEVICE", os.getenv("RFID_YOLO_DEVICE", "cpu"))
        if os.name != "nt" and device == "cpu":
            subprocess.run([str(python), "-m", "pip", "install", "torch", "torchvision",
                            "--index-url", "https://download.pytorch.org/whl/cpu"],
                           check=True, timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([str(python), "-m", "pip", "install", "-r",
                        str(Path(self.cfg["root"]) / "guardian/requirements.txt")],
                       check=True, timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = subprocess.run([str(python), "-m", "unittest", "discover", "-s", "tests"],
                                cwd=self.cfg["root"], timeout=180,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise RuntimeError("RepairedEnvironmentTestsFailed")
        with self.lock:
            self.state.update(previous=old, current=dict(old, python=str(python)), pending=True,
                              trial_started=False, activated_at=time.time())
            self.cfg["python"] = str(python)
            atomic_json(self.path, self.state)
        self.telemetry.event("python_environment_rebuilt")

    def run(self, stop):
        while not stop.is_set():
            try:
                self.stage()
            except Exception as e:
                self.telemetry.event("update_check_failed", error=type(e).__name__)
            stop.wait(self.cfg.get("update_poll_sec", 60))
