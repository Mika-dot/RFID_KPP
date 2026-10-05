import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PATH = Path(__file__).resolve().parents[1] / "deploy/ha/upgrade_initial_windows.py"
SPEC = importlib.util.spec_from_file_location("initial_windows_upgrade", PATH)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


@unittest.skipUnless(shutil.which("git"), "Git is required")
class InitialWindowsCheckoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = (Path(temp.name) / "source").resolve()
        self.source.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Upgrade test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.cache = sorted(upgrade.CACHES)[0]
        self.path = self.source / self.cache
        self.path.parent.mkdir(parents=True)
        self.original = b"\x00original compiled cache"
        self.changed = b"\x00regenerated cache"
        self.path.write_bytes(self.original)
        (self.source / "code.py").write_bytes(b"old source\n")
        self.git("add", ".")
        self.git("commit", "-qm", "old")
        self.old = self.git("rev-parse", "HEAD").decode().strip()
        (self.source / "code.py").write_bytes(b"new source\n")
        self.git("add", ".")
        self.git("commit", "-qm", "new")
        self.target = self.git("rev-parse", "HEAD").decode().strip()
        self.git("reset", "--hard", self.old)
        self.path.write_bytes(self.changed)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.source), *args],
                                       stderr=subprocess.DEVNULL)

    def test_cache_is_saved_byte_for_byte_and_only_cache_restored_before_update(self):
        plan = upgrade.cache_plan(self.source, self.target, self.git)
        backup = self.source.parent / "backup"
        upgrade.save_caches(backup, plan)
        upgrade.restore_caches(self.source, self.git, plan)
        self.assertEqual((backup / "files" / self.cache).read_bytes(), self.changed)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual((self.source / "code.py").read_bytes(), b"old source\n")
        self.git("merge", "--ff-only", self.target)
        self.assertEqual((self.source / "code.py").read_bytes(), b"new source\n")
        self.assertFalse(self.git("status", "--porcelain"))

    def test_real_source_edit_is_preserved(self):
        file = self.source / "code.py"
        file.write_bytes(b"operator edit\n")
        with self.assertRaisesRegex(ValueError, "Non-cache"):
            upgrade.cache_plan(self.source, self.target, self.git)
        self.assertEqual(file.read_bytes(), b"operator edit\n")
        self.assertEqual(self.path.read_bytes(), self.changed)

    def test_staged_cache_is_preserved(self):
        self.git("add", self.cache)
        index = self.git("write-tree")
        with self.assertRaisesRegex(ValueError, "Staged"):
            upgrade.cache_plan(self.source, self.target, self.git)
        self.assertEqual(self.git("write-tree"), index)

    def test_modified_target_cache_is_rejected(self):
        self.git("restore", "--worktree", "--", self.cache)
        self.path.write_bytes(b"\x00changed target cache")
        self.git("add", self.cache)
        self.git("commit", "-qm", "target changes cache")
        target = self.git("rev-parse", "HEAD").decode().strip()
        self.git("reset", "--hard", self.old)
        self.path.write_bytes(self.changed)
        with self.assertRaisesRegex(ValueError, "Target changes"):
            upgrade.cache_plan(self.source, target, self.git)
        self.assertEqual(self.path.read_bytes(), self.changed)

    def test_cache_change_after_inspection_prevents_restore(self):
        plan = upgrade.cache_plan(self.source, self.target, self.git)
        changed_later = self.changed + b"later write"
        self.path.write_bytes(changed_later)
        with self.assertRaisesRegex(ValueError, "after inspection"):
            upgrade.restore_caches(self.source, self.git, plan)
        self.assertEqual(self.path.read_bytes(), changed_later)
