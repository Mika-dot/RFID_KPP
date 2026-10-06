import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HA = ROOT / "deploy/ha"
sys.path.insert(0, str(HA))
SPEC = importlib.util.spec_from_file_location("windows_tool", HA / "windows_tool.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
import environment_tool


def bundle():
    env = {key: "test-value" for key in environment_tool.REQUIRED}
    env.update(KPP_CONN_STR="DRIVER={unchanged};PWD=fixture;",
               KPP_WEB_AUTH_USER="fixture-user", KPP_WEB_AUTH_PASSWORD='!%$`&"пароль',
               RFID_SPOOL_PATH="original-spool.sqlite", KPP_WEB_AUTH_REQUIRED="0")
    return {"version": 1, "ha_token": "a" * 64, "environment": env}


class WindowsDeploymentTests(unittest.TestCase):
    def test_windows_retains_original_connections_paths_and_shared_token(self):
        item = bundle()
        env = tool.windows_environment(item)
        self.assertEqual(item["environment"]["KPP_CONN_STR"], env["PERIMETER_HA_SQL"])
        self.assertEqual("a" * 64, env["PERIMETER_HA_TOKEN"])
        self.assertEqual("original-spool.sqlite", env["RFID_SPOOL_PATH"])
        self.assertEqual("1", env["KPP_WEB_AUTH_REQUIRED"])

    def test_malformed_or_missing_auth_rejected_without_secret_values(self):
        for change in (lambda b: b.update(ha_token="invalid"),
                       lambda b: b["environment"].pop("KPP_WEB_AUTH_PASSWORD"),
                       lambda b: b["environment"].update(SRC_PASSWORD="fixture\0private")):
            item = bundle()
            change(item)
            with self.assertRaises(tool.TransferError) as error:
                tool.windows_environment(item)
            self.assertNotIn("fixture-private", str(error.exception))

    def test_preparation_uses_separate_source_and_keeps_original_spool_and_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "production"
            py = original / "venv64/Scripts/python.exe"
            py32 = original / "RFID_readers/Версия (БД)/venv310_32/Scripts/python.exe"
            for path in (py, py32):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            sentinel = original / "spool.sqlite"
            sentinel.write_bytes(b"existing-production-data")
            private = root / "ha"
            private.mkdir()
            transfer = private / "environment.local.json"
            transfer.write_text(json.dumps(bundle()), encoding="utf-8")
            config = private / "node.json"
            with patch.object(tool.subprocess, "call") as call:
                result = tool.prepare(config, transfer, original)
            call.assert_not_called()
            node = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(str(ROOT), node["root"])
            self.assertEqual(str(py32), node["python32"])
            self.assertFalse(node["controller_enabled"])
            self.assertEqual("http://172.31.0.134:18200", node["nodes"][1]["url"])
            self.assertEqual(b"existing-production-data", sentinel.read_bytes())
            self.assertNotIn("fixture-user", config.read_text(encoding="utf-8"))
            self.assertFalse(result["service_started"])
            before = config.read_bytes()
            with self.assertRaises(tool.TransferError):
                tool.prepare(config, transfer, original)
            self.assertEqual(before, config.read_bytes())

    def test_launch_keeps_credentials_out_of_arguments_and_returns_child_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            config, transfer = self.write_fixture(folder)
            with patch.object(tool.subprocess, "call", return_value=75) as call, \
                    patch.dict(os.environ, {"PERIMETER_HA_TOKEN": "old", "KPP_OLD": "old"}):
                self.assertEqual(75, tool.launch(config, transfer, "serve"))
            argv = call.call_args.args[0]
            env = call.call_args.kwargs["env"]
            self.assertNotIn(bundle()["environment"]["KPP_WEB_AUTH_PASSWORD"], " ".join(argv))
            self.assertEqual("a" * 64, env["PERIMETER_HA_TOKEN"])
            self.assertNotIn("KPP_OLD", env)
            self.assertFalse(call.call_args.kwargs["shell"])

    def test_native_child_receives_unicode_metacharacters_without_shell_expansion(self):
        with tempfile.TemporaryDirectory(prefix="windows deployment ") as folder:
            config, transfer = self.write_fixture(folder)
            root = Path(folder)
            package = root / "guardian"
            package.mkdir()
            (package / "__init__.py").touch()
            expected = repr(bundle()["environment"]["KPP_WEB_AUTH_PASSWORD"])
            (package / "__main__.py").write_text(
                "import os,sys\n"
                f"assert os.environ['KPP_WEB_AUTH_PASSWORD'] == {expected}\n"
                "assert os.environ['PERIMETER_HA_TOKEN'] == 'a'*64\n"
                "assert os.environ['KPP_WEB_AUTH_REQUIRED'] == '1'\n"
                "assert 'KPP_OLD' not in os.environ\n"
                "assert sys.argv[-1] == 'doctor'\n", encoding="utf-8")
            with patch.dict(os.environ, {"KPP_OLD": "old"}):
                self.assertEqual(0, tool.launch(config, transfer, "doctor"))

    def test_physical_controller_is_rejected_before_start(self):
        with tempfile.TemporaryDirectory() as folder:
            config, transfer = self.write_fixture(folder)
            node = json.loads(config.read_text(encoding="utf-8"))
            node["controller_enabled"] = True
            config.write_text(json.dumps(node), encoding="utf-8")
            with patch.object(tool.subprocess, "call") as call:
                with self.assertRaises(tool.TransferError):
                    tool.launch(config, transfer, "serve")
            call.assert_not_called()

    @staticmethod
    def write_fixture(folder):
        root = Path(folder)
        config = root / "node.json"
        config.write_text(json.dumps({"node_id": "physical", "controller_enabled": False,
                                     "python": sys.executable, "root": str(root)}), encoding="utf-8")
        transfer = root / "environment.local.json"
        transfer.write_text(json.dumps(bundle()), encoding="utf-8")
        return config, transfer

    @unittest.skipUnless(os.name == "nt", "Requires native Windows PowerShell installation")
    def test_native_installer_registers_separate_checkout_without_starting_or_changing_packages(self):
        # Mock OS scheduling/firewall writes, execute actual PS argument construction.
        with tempfile.TemporaryDirectory(prefix="installer test ") as folder:
            root = Path(folder)
            config, transfer = self.write_fixture(folder)
            node = json.loads(config.read_text(encoding="utf-8"))
            node["root"] = str(ROOT)
            config.write_text(json.dumps(node), encoding="utf-8")
            script = root / "fixture.ps1"
            script.write_text(r'''
$ErrorActionPreference='Stop'
$global:HA_TEST_REGISTERED=$false
function Get-ScheduledTask { return $null }
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory)
 if ($Argument -notlike '*run-windows.ps1*' -or $Argument -notlike '*-Bundle*') { throw 'Bad task arguments' }
 if ($Argument -like '*fixture-user*') { throw 'Secret argument' }
 return @{} }
function New-ScheduledTaskTrigger { param([switch]$AtStartup) return @{} }
function New-ScheduledTaskSettingsSet { param($RestartCount,$RestartInterval,$ExecutionTimeLimit,$MultipleInstances) return @{} }
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel)
 if ($UserId -ne 'SYSTEM') { throw 'Wrong account' }; return @{} }
function Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Settings,$Principal,[switch]$Force)
 $global:HA_TEST_REGISTERED=$true }
function Start-ScheduledTask { throw 'Installer must not start tasks' }
function Get-NetFirewallRule { return @{ Present=$true } }
& $env:TEST_INSTALLER -Root $env:TEST_PRODUCTION -Config $env:TEST_NODE -Bundle $env:TEST_BUNDLE
if (-not $global:HA_TEST_REGISTERED) { throw 'Task was not registered' }
''', encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
            env.update(TEST_INSTALLER=str(HA / "install-windows.ps1"), TEST_NODE=str(config),
                       TEST_BUNDLE=str(transfer), TEST_PRODUCTION=str(root / "old-production"))
            env["PSModulePath"] = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/Modules")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                     "-File", str(script)], env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
