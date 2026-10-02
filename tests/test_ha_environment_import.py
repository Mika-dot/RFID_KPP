import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "environment_tool_import", ROOT / "deploy/ha/environment_tool.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def bundle():
    env = {key: "fixture" for key in tool.REQUIRED}
    for key in ("RFID_DB_CONNECTION", "KPP_CONN_STR", "KPP_WEB_DB_CONNECTION", "KPP_TASK_CONN_STR"):
        env[key] = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=fixture;PWD={p;DRIVER=fake}}word};"
    env.update(RFID_MODEL_PATH="D:\\physical\\best.pt", RFID_YOLO_DEVICE="0",
               KPP_WEB_AUTH_USER="fixture-user", KPP_WEB_AUTH_PASSWORD="p!$`\\\"#'пароль",
               KPP_NON_REEL_TAGS_FILE="D:\\physical\\deploy\\non_reel_tags.txt",
               KPP_MIGRATION_TRUSTED_CONN="windows-only")
    return {"version": 1, "ha_token": "a" * 64, "environment": env}


def config(root="/opt/perimeter/source", node="perimetr"):
    return {"node_id": node, "root": str(root), "python": sys.executable,
            "env": {"RFID_SDK_MODE": "wine", "RFID_YOLO_DEVICE": "cpu",
                    "RFID_MODEL_PATH": str(root) + "/model.pt"}}


class EnvironmentImportTests(unittest.TestCase):
    def test_real_driver_changes_but_driver_text_inside_password_is_preserved(self):
        original = "PWD={p;DRIVER=fake}}word}; DRIVER = {old-driver} ;SERVER=fixture;"
        result = tool.linux_odbc(original)
        self.assertEqual("PWD={p;DRIVER=fake}}word}; DRIVER = {ODBC Driver 18 for SQL Server} ;SERVER=fixture;", result)

    def test_ambiguous_and_malformed_connections_fail_without_secret_values(self):
        for value in ("PWD={private", "DSN=private", "DRIVER=a;DRIVER=b;PWD=private",
                      "DRIVER={old}garbage;PWD=private", "DRIVER=a;invalid;PWD=private"):
            with self.assertRaises(tool.TransferError) as caught:
                tool.linux_odbc(value)
            self.assertNotIn("private", str(caught.exception))

    def test_both_nodes_keep_token_and_native_settings_without_mutating_bundle(self):
        source = bundle()
        before = json.dumps(source)
        for node in ("perimetr", "comparator"):
            env = tool.linux_environment(source, config(node=node))
            self.assertEqual("a" * 64, env["PERIMETER_HA_TOKEN"])
            self.assertEqual(env["KPP_CONN_STR"], env["PERIMETER_HA_SQL"])
            self.assertEqual("cpu", env["RFID_YOLO_DEVICE"])
            self.assertEqual("wine", env["RFID_SDK_MODE"])
            self.assertEqual("/opt/perimeter/source/model.pt", env["RFID_MODEL_PATH"])
            self.assertEqual("/opt/perimeter/source/deploy/non_reel_tags.txt", env["KPP_NON_REEL_TAGS_FILE"])
            self.assertEqual("1", env["KPP_WEB_AUTH_REQUIRED"])
            self.assertNotIn("KPP_MIGRATION_TRUSTED_CONN", env)
        self.assertEqual(before, json.dumps(source))

    def test_missing_web_credentials_and_unmapped_windows_paths_are_rejected(self):
        source = bundle()
        source["environment"].pop("KPP_WEB_AUTH_PASSWORD")
        with self.assertRaises(tool.TransferError):
            tool.linux_environment(source, config())
        source = bundle()
        source["environment"]["RFID_EXTRA_PATH"] = "D:\\private\\file"
        with self.assertRaises(tool.TransferError) as caught:
            tool.linux_environment(source, config())
        self.assertIn("RFID_EXTRA_PATH", str(caught.exception))
        self.assertNotIn("D:", str(caught.exception))

    def test_environment_special_characters_are_literal_and_no_shell_code_runs(self):
        env = {"TEST_PASSWORD": "!$HOME`command`\\\"#'пароль;%(name)s"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "environment"
            path.write_text(tool.environment_text(env), encoding="utf-8")
            self.assertEqual(env, tool.read_generated_environment(path))
        for value in ("private\nINJECT=1", "private\r", "private\0"):
            with self.assertRaises(tool.TransferError) as caught:
                tool.environment_text({"TEST_PASSWORD": value})
            self.assertNotIn("private", str(caught.exception))

    def test_web_prompt_is_local_and_reexport_reuses_password_and_token(self):
        source = bundle()["environment"]
        source.pop("KPP_WEB_AUTH_USER")
        source.pop("KPP_WEB_AUTH_PASSWORD")
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "environment.local.json"
            with patch.object(tool, "capture_config", return_value=source.copy()), patch("builtins.input", return_value=""), \
                    patch.object(tool.getpass, "getpass", side_effect=["new-private", "new-private"]):
                tool.export_bundle("config_v3.cmd", output, web_auth=True)
            first = json.loads(output.read_text(encoding="utf-8"))
            self.assertNotIn("KPP_WEB_AUTH_PASSWORD", source)
            with patch.object(tool, "capture_config", return_value=source.copy()), \
                    patch("builtins.input") as login, patch.object(tool.getpass, "getpass") as password:
                report = tool.export_bundle("config_v3.cmd", output, web_auth=True)
            login.assert_not_called()
            password.assert_not_called()
            second = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(first["ha_token"], second["ha_token"])
            self.assertEqual("new-private", second["environment"]["KPP_WEB_AUTH_PASSWORD"])
            self.assertNotIn("new-private", json.dumps(report))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Requires native Linux ownership")
    def test_failed_atomic_write_preserves_previous_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "environment"
            path.write_text("previous", encoding="utf-8")
            with patch.object(tool.os, "replace", side_effect=PermissionError("fixture")):
                with self.assertRaises(PermissionError):
                    tool.atomic_environment(path, "new", os.getuid(), os.getgid())
            self.assertEqual("previous", path.read_text())
            self.assertEqual([path], list(path.parent.iterdir()))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Requires native Linux ownership")
    def test_install_preserves_backup_owner_and_does_not_start_service(self):
        user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "bundle.local.json"
            node = root / "node.json"
            destination = root / "environment"
            source.write_text(json.dumps(bundle()), encoding="utf-8")
            node.write_text(json.dumps(config()), encoding="utf-8")
            destination.write_text("previous", encoding="utf-8")
            with patch.object(tool.os, "geteuid", return_value=0), \
                    patch("pwd.getpwnam", return_value=user), \
                    patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=3)) as run:
                report = tool.install_bundle(source, node, destination)
                tool.install_bundle(source, node, destination)
            self.assertTrue(report["installed"])
            self.assertFalse(report["service_started"])
            self.assertEqual("previous", (root / "environment.pre-import").read_text())
            self.assertEqual(0o600, destination.stat().st_mode & 0o777)
            self.assertEqual(user.pw_uid, destination.stat().st_uid)
            self.assertEqual("a" * 64, tool.read_generated_environment(destination)["PERIMETER_HA_TOKEN"])
            self.assertTrue(all(call.args[0][1] == "is-active" for call in run.call_args_list))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Requires native Linux")
    def test_active_guardian_blocks_import_before_writes(self):
        with patch.object(tool.os, "geteuid", return_value=0), \
                patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                patch.object(tool, "atomic_environment") as write:
            with self.assertRaises(tool.TransferError):
                tool.install_bundle("missing-bundle", "missing-node", "missing-environment")
        write.assert_not_called()

    def test_doctor_credentials_are_environment_only_and_return_code_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            node = Path(folder) / "node.json"
            environment = Path(folder) / "environment"
            node.write_text(json.dumps(config()), encoding="utf-8")
            environment.write_text(tool.environment_text({"SRC_PASSWORD": "fixture-private"}), encoding="utf-8")
            with patch.object(tool.subprocess, "run", return_value=SimpleNamespace(returncode=2)) as run:
                self.assertEqual(2, tool.run_doctor(node, environment))
            self.assertNotIn("fixture-private", " ".join(run.call_args.args[0]))
            self.assertEqual("fixture-private", run.call_args.kwargs["env"]["SRC_PASSWORD"])

    @unittest.skipUnless(sys.platform.startswith("linux") and Path("/run/systemd/system").is_dir(),
                         "Requires a running native systemd")
    def test_generated_file_is_parsed_by_real_systemd_without_expansion(self):
        env = {"PERIMETER_TEST_VALUE": "!$HOME`false`\\\"#'пароль;%(name)s"}
        sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "environment"
            path.write_text(tool.environment_text(env), encoding="utf-8")
            path.chmod(0o600)
            result = subprocess.run(sudo + ["systemd-run", "--quiet", "--wait", "--pipe",
                "--property=EnvironmentFile=" + str(path), sys.executable, "-c",
                "import json,os; print(json.dumps({'PERIMETER_TEST_VALUE':os.environ['PERIMETER_TEST_VALUE']}))"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(env, json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
