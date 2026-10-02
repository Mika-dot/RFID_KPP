import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "environment_tool", ROOT / "deploy" / "ha" / "environment_tool.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def complete_env():
    env = {key: "test-value" for key in tool.REQUIRED}
    env["KPP_CONN_STR"] = "DRIVER={ODBC Driver 17 for SQL Server};PWD=test-private-value;"
    return env


class EnvironmentExportTests(unittest.TestCase):
    def test_legacy_alias_fills_missing_connections_without_overwriting_explicit(self):
        result = tool.normalized_environment({
            "COMMON_DB_CONN": "legacy-private", "RFID_DB_CONNECTION": "explicit-private"})
        self.assertEqual("explicit-private", result["RFID_DB_CONNECTION"])
        self.assertEqual("explicit-private", result["KPP_CONN_STR"])
        self.assertEqual("explicit-private", result["KPP_WEB_DB_CONNECTION"])
        legacy = tool.normalized_environment({"COMMON_DB_CONN": "legacy-private"})
        self.assertEqual("legacy-private", legacy["RFID_DB_CONNECTION"])

    def test_capture_filters_unrelated_and_old_ha_secrets(self):
        result = tool.selected_environment({
            "OTHER_API_KEY": "unrelated-private", "PERIMETER_HA_TOKEN": "old-private",
            "PERIMETER_CAPTURE_PRIVATE_DIR": "private-dir", "rfid_reader_ip": "test-ip",
            "SRC_PASSWORD": "source-private"})
        self.assertEqual({"RFID_READER_IP": "test-ip", "SRC_PASSWORD": "source-private"}, result)

    def test_missing_report_contains_names_only(self):
        env = complete_env()
        env["RFID_RTSP_0"] = "rtsp://<CAMERA_USER>:<CAMERA_PASSWORD>@example"
        env["SRC_PASSWORD"] = ""
        self.assertEqual(["RFID_RTSP_0", "SRC_PASSWORD"], tool.missing_fields(env))

    def test_reexport_preserves_common_token_and_never_prints_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "private" / "environment.local.json"
            with patch.object(tool, "capture_config", return_value=complete_env()):
                first = tool.export_bundle("config_v3.cmd", output)
                token = json.loads(output.read_text(encoding="utf-8"))["ha_token"]
                second = tool.export_bundle("config_v3.cmd", output)
            self.assertEqual(token, json.loads(output.read_text(encoding="utf-8"))["ha_token"])
            self.assertRegex(token, r"^[0-9a-f]{64}$")
            self.assertNotIn("private-value", json.dumps([first, second]))
            self.assertNotIn(token, json.dumps([first, second]))
            if os.name != "nt":
                self.assertEqual(0o600, output.stat().st_mode & 0o777)
                self.assertEqual(0o700, output.parent.stat().st_mode & 0o777)

    def test_failed_capture_preserves_previous_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "environment.local.json"
            old = {"version": 1, "ha_token": "a" * 64, "environment": complete_env()}
            output.write_text(json.dumps(old), encoding="utf-8")
            before = output.read_bytes()
            with patch.object(tool, "capture_config", side_effect=RuntimeError("private-error")):
                with self.assertRaises(RuntimeError):
                    tool.export_bundle("config_v3.cmd", output)
            self.assertEqual(before, output.read_bytes())

    def test_missing_config_does_not_replace_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "environment.local.json"
            with patch.object(tool, "capture_config", return_value=complete_env()):
                tool.export_bundle("config_v3.cmd", output)
            before = output.read_bytes()
            with patch.object(tool, "capture_config", return_value={}):
                with self.assertRaises(tool.TransferError):
                    tool.export_bundle("config_v3.cmd", output)
            self.assertEqual(before, output.read_bytes())

    def test_profile_difference_has_field_names_not_values(self):
        first = complete_env()
        second = complete_env()
        second["SRC_PASSWORD"] = "different-private-value"
        with patch.object(tool, "capture_config", side_effect=[first, second]):
            result = tool.profile_reports(ROOT, ROOT)
        self.assertEqual(["SRC_PASSWORD"], result["different_variables"])
        self.assertNotIn("private-value", json.dumps(result))

    def test_generic_cli_failure_is_redacted(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(stream):
            with patch.object(tool, "export_bundle", side_effect=ValueError("password=private-value")):
                code = tool.main(["export", "--config", "config_v3.cmd", "--output",
                                  str(Path(folder) / "environment.local.json")])
        self.assertEqual(2, code)
        self.assertEqual({"error": "ValueError"}, json.loads(stream.getvalue()))

    def test_rejects_cmd_path_expansion_and_line_injection(self):
        for path in ("bad%PATH%.cmd", "bad!name.cmd", "bad^name.cmd", "bad\nname.cmd"):
            with self.assertRaises(tool.TransferError):
                tool.cmd_path(path)

    @unittest.skipUnless(os.name == "nt", "Requires native Windows CMD and ACLs")
    def test_real_cmd_capture_resolves_aliases_and_keeps_unicode_and_exclamation(self):
        with tempfile.TemporaryDirectory(prefix="capture test ") as folder:
            root = Path(folder)
            config = root / "deploy" / "config_v3.cmd"
            config.parent.mkdir()
            private = tool.private_directory(root / "private")
            config.write_bytes((
                '@echo off\r\nset "SQL_CONN=DRIVER={test};PWD=p!$&private;"\r\n'
                'set "KPP_CONN_STR=%SQL_CONN%"\r\n'
                'set "SRC_PASSWORD=пароль!тест"\r\n'
                'set "RFID_MODEL_PATH=%ROOT%\\test.pt"\r\n'
            ).encode("utf-8"))
            with patch.dict(os.environ, {"RFID_DB_CONNECTION": "stale-parent-private"}):
                env = tool.capture_config(config, private)
            self.assertEqual("DRIVER={test};PWD=p!$&private;", env["KPP_CONN_STR"])
            self.assertEqual(env["KPP_CONN_STR"], env["RFID_DB_CONNECTION"])
            self.assertEqual("пароль!тест", env["SRC_PASSWORD"])
            self.assertEqual(str(root) + "\\test.pt", env["RFID_MODEL_PATH"])
            self.assertEqual([], list(private.iterdir()))


if __name__ == "__main__":
    unittest.main()
