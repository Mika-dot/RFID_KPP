"""Release admission against real local git trees, without SQL or readers."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from guardian.release_contract import BASE_FLOOR, MANIFEST, verify_business
from guardian.update import Updates


class ReleaseAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.repo = self.folder / "source"
        self.repo.mkdir()
        self.git("init")
        self.git("config", "user.name", "Release admission test")
        self.git("config", "user.email", "release-test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.write("guardian/__main__.py", "")
        self.write("guardian/sql.py", 'FENCING_PROTOCOL = 2\nEPOCH_BARRIER = "Perimeter.HA.Epoch"\n')
        self.write(MANIFEST, json.dumps(BASE_FLOOR))
        source = Path(__file__).parents[1] / "common/business_flow.py"
        self.write("common/business_flow.py", source.read_text(encoding="utf-8"))
        self.write("tests/test_candidate.py", "import unittest\nclass Candidate(unittest.TestCase):\n"
                   "    def test_candidate_owned_suite(self):\n        self.assertTrue(True)\n")
        self.base = self.commit()
        self.cfg = {"root": str(self.repo), "state_dir": str(self.folder / "state"),
                    "update_source": str(self.repo), "release_dir": str(self.folder / "releases"),
                    "python": sys.executable}
        self.telemetry = Mock()
        self.updates = Updates(self.cfg, Mock(), self.telemetry)
        self.updates.state["trusted_main_sha"] = self.base
        self.updates.state["fencing_protocol_min"] = 2

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-m", "Local release fixture")
        return self.git("rev-parse", "HEAD")

    def candidate(self):
        sha = self.commit()
        self.git("checkout", "--detach", self.base)
        real_git = self.updates.git

        def fetched_git(*args, **kwargs):
            if args == ("fetch", "origin", "main"):
                return ""
            if args == ("rev-parse", "FETCH_HEAD"):
                return sha
            return real_git(*args, **kwargs)

        self.updates.git = fetched_git
        return sha

    def test_markdown_only_advances_observed_main_without_runtime_activation(self):
        self.write("deploy/ha/acceptance.md", "Recorded acceptance boundary.\n")
        sha = self.candidate()
        self.updates.staged = {"sha": "f" * 40, "root": "older candidate"}
        with patch("guardian.update.preflight") as preflight:
            self.updates.stage()
        preflight.assert_not_called()
        self.assertIsNone(self.updates.staged)
        self.assertFalse(self.updates.activate())
        self.assertEqual(self.base, self.updates.main_sha)
        self.assertEqual(self.base, self.updates.state["current"]["sha"])
        self.assertEqual(sha, self.updates.state["docs_only_sha"])
        self.assertFalse((self.folder / "releases").exists())
        restored = Updates(self.cfg, Mock(), Mock())
        self.assertEqual(sha, restored.state["trusted_main_sha"])
        self.assertEqual(self.base, restored.state["current"]["sha"])

    @patch("guardian.update.preflight", return_value={"ok": True})
    def test_operator_script_change_is_runtime_even_inside_documentation_folder(self, preflight):
        self.write("deploy/ha/operator.py", "ENABLED = True\n")
        sha = self.candidate()
        self.updates.stage()
        self.assertEqual(sha, self.updates.staged["sha"])
        preflight.assert_called_once()
        self.assertEqual(BASE_FLOOR["business_generation"],
                         self.updates.state["runtime_contract_floor"]["business_generation"])

    def test_missing_manifest_is_quarantined_before_worktree_or_candidate_execution(self):
        (self.repo / MANIFEST).unlink()
        sha = self.candidate()
        with self.assertRaisesRegex(RuntimeError, "CandidateRuntimeContractMissing"):
            self.updates.stage()
        self.assertFalse((self.folder / "releases").exists())
        restored = Updates(self.cfg, Mock(), Mock())
        self.assertIn(sha, restored.quarantined)
        self.telemetry.event.assert_called_with("update_candidate_rejected", sha=sha,
                                               reason="CandidateRuntimeContractMissing")

    def test_removed_hotfix_capability_is_rejected_despite_main_ancestry(self):
        value = dict(BASE_FLOOR, capabilities=["rfid-causal-activity-evidence"])
        self.write(MANIFEST, json.dumps(value))
        sha = self.candidate()
        with self.assertRaisesRegex(RuntimeError, "CandidateBusinessDowngrade"):
            self.updates.stage()
        self.assertIn(sha, self.updates.quarantined)
        self.assertFalse((self.folder / "releases").exists())

    def test_candidate_claiming_capabilities_cannot_remove_historical_latch_behavior(self):
        # A candidate-owned green test suite and manifest cannot replace the
        # checks shipped with the installed release.
        path = self.repo / "common/business_flow.py"
        self.write("common/business_flow.py", path.read_text(encoding="utf-8") +
                   '\n_original = assess_rfid_flow\ndef assess_rfid_flow(**values):\n'
                   '    values["video_history_complete"] = False\n    return _original(**values)\n')
        sha = self.candidate()
        with patch("guardian.update.preflight") as preflight:
            with self.assertRaisesRegex(RuntimeError, "CandidateProtectedBusinessChecksFailed"):
                self.updates.stage()
        preflight.assert_not_called()
        self.assertIsNone(self.updates.staged)
        self.assertIn(sha, self.updates.quarantined)
        candidate_root = self.folder / "releases" / sha
        green = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                               cwd=candidate_root, capture_output=True)
        self.assertEqual(0, green.returncode)

    def test_candidate_cannot_count_activity_from_before_the_last_read_as_a_fault(self):
        path = self.repo / "common/business_flow.py"
        self.write("common/business_flow.py", path.read_text(encoding="utf-8") +
                   '\ndef _after(value, baseline):\n    return value is not None\n')
        self.candidate()
        with self.assertRaisesRegex(RuntimeError, "CandidateProtectedBusinessChecksFailed"):
            self.updates.stage()

    @patch("guardian.update.preflight", return_value={"ok": True})
    def test_unconfirmed_higher_generation_rolls_back_without_pinning_the_floor(self, preflight):
        self.write(MANIFEST, json.dumps(dict(BASE_FLOOR, business_generation=2)))
        self.candidate()
        self.updates.stage()
        self.updates.activate()
        restarted = Updates(self.cfg, Mock(), Mock())
        self.assertEqual(1, restarted.state["runtime_contract_floor"]["business_generation"])
        restarted.rollback()
        self.assertEqual(self.base, restarted.state["current"]["sha"])
        self.assertEqual(1, restarted.state["runtime_contract_floor"]["business_generation"])

    @patch("guardian.update.preflight", return_value={"ok": True})
    def test_confirmed_generation_floor_survives_restart_and_rejects_old_generation(self, preflight):
        future = dict(BASE_FLOOR, business_generation=2,
                      capabilities=BASE_FLOOR["capabilities"] + ["future-qualified-behavior"])
        self.write(MANIFEST, json.dumps(future))
        sha = self.candidate()
        self.updates.stage()
        self.updates.activate()
        self.updates.confirm()
        restarted = Updates(self.cfg, Mock(), Mock())
        self.assertEqual(2, restarted.state["runtime_contract_floor"]["business_generation"])
        self.assertIn("future-qualified-behavior", restarted.state["runtime_contract_floor"]["capabilities"])
        # Commit a downgrade on top of the accepted main, so ancestry is valid.
        self.git("checkout", "--detach", sha)
        self.write(MANIFEST, json.dumps(BASE_FLOOR))
        downgrade = self.commit()
        real_git = restarted.git

        def fetched_git(*args, **kwargs):
            if args == ("fetch", "origin", "main"):
                return ""
            if args == ("rev-parse", "FETCH_HEAD"):
                return downgrade
            return real_git(*args, **kwargs)

        restarted.git = fetched_git
        with self.assertRaisesRegex(RuntimeError, "CandidateBusinessDowngrade"):
            restarted.stage()

    def test_installed_business_checks_keep_both_factory_hotfixes(self):
        self.assertEqual(6, verify_business(self.repo))


if __name__ == "__main__":
    unittest.main()
