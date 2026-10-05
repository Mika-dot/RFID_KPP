import importlib.util
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PATH = Path(__file__).resolve().parents[1] / "deploy/ha/upgrade_initial_windows.py"
SPEC = importlib.util.spec_from_file_location("initial_windows_upgrade", PATH)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class ExistingLegacyFaultTests(unittest.TestCase):
    def health(self, latched=True):
        deps = {
            "RfidReader": ("business_flow", "database", "delivery_writer",
                           "local_spool", "reader_loop", "rfid_reader", "rfid_tcp"),
            "RusGuardSync": ("destination_database", "source_database", "sync_loop"),
            "Yolo": ("camera_0", "camera_1", "database", "delivery_writer",
                     "local_spool", "model", "pipeline"),
            "Aggregator": ("database", "pipeline", "rfid_reader", "rusguard", "yolo"),
            "WebDashboard": ("aggregator", "database", "web_port"),
        }
        health = {name: {"ok": True, "detail": {
            "status": "ok", "dependencies": {
                key: {"status": "ok"} for key in names
            }}} for name, names in deps.items()}
        if latched:
            for name, key, reason in (
                ("RfidReader", "business_flow", "rfid_business_flow_fault_latched"),
                ("Aggregator", "rfid_reader", "peer_not_ready"),
                ("WebDashboard", "aggregator", "peer_not_ready"),
            ):
                health[name]["ok"] = False
                health[name]["detail"]["status"] = "degraded"
                health[name]["detail"]["dependencies"][key] = {
                    "status": "unavailable", "detail": reason,
                }
        return health

    def test_default_still_rejects_latched_business_flow(self):
        with self.assertRaisesRegex(ValueError, "not all healthy"):
            upgrade.legacy_health_mode(self.health())

    def test_explicit_maintenance_mode_keeps_red_statuses_unchanged(self):
        health = self.health()
        before = deepcopy(health)
        self.assertEqual(upgrade.legacy_health_mode(health, True), "existing_latched_rfid")
        self.assertEqual(health, before)

    def test_healthy_services_need_no_exception(self):
        for allowed in (False, True):
            self.assertEqual(upgrade.legacy_health_mode(self.health(False), allowed), "healthy")

    def test_cascade_heartbeat_lag_is_allowed(self):
        health = self.health()
        healthy = self.health(False)
        health["Aggregator"] = healthy["Aggregator"]
        health["WebDashboard"] = healthy["WebDashboard"]
        self.assertEqual(upgrade.legacy_health_mode(health, True), "existing_latched_rfid")

    def test_transport_storage_and_other_service_faults_are_rejected(self):
        for name, deps in (
            ("RfidReader", ("database", "delivery_writer", "local_spool", "reader_loop",
                            "rfid_reader", "rfid_tcp")),
            ("Aggregator", ("database", "pipeline", "rusguard", "yolo")),
            ("WebDashboard", ("database", "web_port")),
        ):
            for key in deps:
                with self.subTest(service=name, dependency=key):
                    health = self.health()
                    health[name]["detail"]["dependencies"][key]["status"] = "unavailable"
                    with self.assertRaises(ValueError):
                        upgrade.legacy_health_mode(health, True)
        for name in ("RusGuardSync", "Yolo"):
            with self.subTest(service=name):
                health = self.health()
                health[name]["ok"] = False
                with self.assertRaises(ValueError):
                    upgrade.legacy_health_mode(health, True)

    def test_unknown_missing_and_probe_failed_dependencies_are_rejected(self):
        changes = (
            lambda h: h["RfidReader"].update(error="TimeoutError"),
            lambda h: h["RfidReader"]["detail"]["dependencies"].pop("local_spool"),
            lambda h: h["RfidReader"]["detail"]["dependencies"].update(
                unexpected={"status": "unavailable"}),
            lambda h: h["RfidReader"]["detail"]["dependencies"]["business_flow"].update(
                detail="probe_OperationalError"),
            lambda h: h["Aggregator"]["detail"]["dependencies"]["rfid_reader"].update(
                detail="peer_unreachable"),
            lambda h: h.pop("Yolo"),
            lambda h: h.update(unexpected={"ok": True}),
        )
        for change in changes:
            with self.subTest(change=change):
                health = self.health()
                change(health)
                with self.assertRaises(ValueError):
                    upgrade.legacy_health_mode(health, True)

    def test_new_latch_is_rejected_if_initial_services_were_healthy(self):
        with self.assertRaisesRegex(ValueError, "worsened"):
            upgrade.preserve_health_mode("healthy", "existing_latched_rfid")
        upgrade.preserve_health_mode("existing_latched_rfid", "existing_latched_rfid")
        upgrade.preserve_health_mode("existing_latched_rfid", "healthy")


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
