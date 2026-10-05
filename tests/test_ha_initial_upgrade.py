import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy/ha/upgrade_initial_vm.py"
SPEC = importlib.util.spec_from_file_location("initial_vm_upgrade", MODULE_PATH)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


@unittest.skipUnless(shutil.which("git"), "Git is required for checkout upgrade tests")
class InitialCheckoutUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = (Path(self.temp.name) / "source").resolve()
        self.source.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Upgrade test")
        self.git("config", "user.email", "test@example.invalid")
        (self.source / ".gitattributes").write_bytes(b"*.py text eol=lf\n*.cmd text eol=crlf\n")
        (self.source / ".gitignore").write_bytes(b"*.local.json\n")
        self.git("add", ".gitattributes", ".gitignore")
        self.legacy = "legacy with spaces.py"
        for name, data in ((self.legacy, b"\xef\xbb\xbfprint('old')\r\n"),
                           ("legacy.cmd", b"@echo off\r\n")):
            blob = self.git("hash-object", "-w", "--stdin", data=data).decode().strip()
            self.git("update-index", "--add", "--cacheinfo", "100644", blob, name)
            (self.source / name).write_bytes(data)
        self.git("commit", "-qm", "old blobs with CRLF")
        self.old = self.git("rev-parse", "HEAD").decode().strip()
        self.git("add", "--renormalize", "--", self.legacy, "legacy.cmd")
        (self.source / "guardian.py").write_bytes(b"print('new')\n")
        self.git("add", "guardian.py")
        self.git("commit", "-qm", "normalized target with agent update")
        self.target = self.git("rev-parse", "HEAD").decode().strip()
        self.git("reset", "--hard", self.old)
        (self.source / "environment.local.json").write_bytes(b"private fixture")

    def git(self, *args, data=None):
        return subprocess.check_output(["git", "-C", str(self.source), *args],
                                       input=data, stderr=subprocess.DEVNULL)

    def test_false_dirty_clone_upgrades_and_keeps_private_files_and_backup(self):
        self.assertTrue(self.git("status", "--porcelain"))
        plan = upgrade.checkout_plan(self.source, self.target, self.git)
        self.assertEqual({p[0] for p in plan}, {self.legacy, "legacy.cmd"})
        backup = Path(self.temp.name) / "backup"
        upgrade.save_checkout(self.source, backup, plan)
        upgrade.merge_checkout(self.source, self.target, self.git, plan)
        self.assertFalse(self.git("status", "--porcelain"))
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), self.target)
        self.assertTrue((self.source / "guardian.py").is_file())
        self.assertEqual((self.source / "environment.local.json").read_bytes(), b"private fixture")
        for name, original, _ in plan:
            self.assertEqual((backup / "files" / name).read_bytes(), original)

    def test_real_edit_is_preserved_and_index_is_unchanged(self):
        path = self.source / self.legacy
        content = path.read_bytes() + b"print('operator change')\n"
        path.write_bytes(content)
        index = self.git("write-tree")
        with self.assertRaisesRegex(ValueError, "Content changes"):
            upgrade.checkout_plan(self.source, self.target, self.git)
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.git("write-tree"), index)

    def test_staged_change_is_preserved(self):
        (self.source / "operator.txt").write_bytes(b"keep this")
        self.git("add", "operator.txt")
        index = self.git("write-tree")
        with self.assertRaisesRegex(ValueError, "Staged changes"):
            upgrade.checkout_plan(self.source, self.target, self.git)
        self.assertEqual(self.git("write-tree"), index)

    def test_untracked_file_is_preserved(self):
        path = self.source / "operator.txt"
        path.write_bytes(b"keep this")
        with self.assertRaisesRegex(ValueError, "Untracked files"):
            upgrade.checkout_plan(self.source, self.target, self.git)
        self.assertEqual(path.read_bytes(), b"keep this")

    def test_edit_after_inspection_aborts_before_staging(self):
        plan = upgrade.checkout_plan(self.source, self.target, self.git)
        path = self.source / self.legacy
        content = path.read_bytes() + b"print('later edit')\n"
        path.write_bytes(content)
        index = self.git("write-tree")
        with self.assertRaisesRegex(ValueError, "after inspection"):
            upgrade.merge_checkout(self.source, self.target, self.git, plan)
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.git("write-tree"), index)

    @unittest.skipIf(os.name == "nt", "Windows does not expose the Unix executable bit")
    def test_mode_change_is_preserved(self):
        path = self.source / self.legacy
        path.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "mode/deletion"):
            upgrade.checkout_plan(self.source, self.target, self.git)
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)
