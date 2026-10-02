"""Capture the physical node's local CMD settings without printing credentials.

Run with native Windows Python. Exported *.local.json files stay outside Git.
The CMD config runs in a child process; the caller's environment is unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


PREFIXES = ("RFID_", "KPP_", "SRC_", "DST_", "RUSGUARD_", "PERIMETER_")
ALIASES = {"SQL_CONN", "COMMON_DB_CONN"}
REQUIRED = (
    "RFID_DB_CONNECTION", "KPP_CONN_STR", "KPP_WEB_DB_CONNECTION",
    "RFID_READER_IP", "RFID_RTSP_0", "RFID_RTSP_1",
    "SRC_SERVER", "SRC_DATABASE", "SRC_USERNAME", "SRC_PASSWORD",
    "DST_SERVER", "DST_DATABASE", "DST_USERNAME", "DST_PASSWORD",
)
PROFILES = {
    "v3": "deploy/config_v3.cmd",
    "legacy": "autostart/kpp_env_config_FINAL.cmd",
}


class TransferError(Exception):
    """Messages in this class contain field names, never field values."""


def selected_environment(env):
    return {
        key.upper(): value for key, value in env.items()
        if (key.upper().startswith(PREFIXES) or key.upper() in ALIASES)
        and not key.upper().startswith("PERIMETER_HA_")
        and key.upper() != "PERIMETER_CAPTURE_PRIVATE_DIR"
    }


def normalized_environment(env):
    result = selected_environment(env)
    connection = next((result.get(key) for key in (
        "KPP_CONN_STR", "RFID_DB_CONNECTION", "COMMON_DB_CONN", "SQL_CONN"
    ) if result.get(key)), None)
    if connection:
        for key in ("RFID_DB_CONNECTION", "KPP_CONN_STR", "KPP_WEB_DB_CONNECTION"):
            result.setdefault(key, connection)
    return result


def missing_fields(env):
    def valid(value):
        if not isinstance(value, str) or not value.strip():
            return False
        return not any(marker in value for marker in (
            "<SQL_", "<CAMERA_", "<RFID_", "LOCAL_SECRET", "LOCAL_SOURCE_",
            "LOCAL_WRITER_", "CHANGE_TO_", "SQL_SERVER;",
        ))
    return [name for name in REQUIRED if not valid(env.get(name))]


def private_directory(path):
    path = Path(path).absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Do not redirect credential capture through a link or Windows junction.
    if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise TransferError("Private directory must not be a link")
    if os.name != "nt":
        path.chmod(0o700)
        return path
    # Replace the DACL, including any explicit old grants. Local administrators
    # and SYSTEM retain access alongside the current user's SID.
    command = (
        "$ErrorActionPreference='Stop';"
        "$acl=Get-Acl -LiteralPath $env:PERIMETER_CAPTURE_PRIVATE_DIR;"
        "$acl.SetAccessRuleProtection($true,$false);"
        "foreach($old in @($acl.Access)){$acl.RemoveAccessRuleSpecific($old)};"
        "$current=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
        "$ids=@($current,'S-1-5-18','S-1-5-32-544');"
        "foreach($text in $ids){"
        "$sid=[System.Security.Principal.SecurityIdentifier]::new($text);"
        "$rule=[System.Security.AccessControl.FileSystemAccessRule]::new("
        "$sid,[System.Security.AccessControl.FileSystemRights]::FullControl,"
        "[System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',"
        "[System.Security.AccessControl.PropagationFlags]::None,"
        "[System.Security.AccessControl.AccessControlType]::Allow);"
        "$acl.AddAccessRule($rule)};"
        "Set-Acl -LiteralPath $env:PERIMETER_CAPTURE_PRIVATE_DIR -AclObject $acl"
    )
    # A caller running PowerShell 7 can export its PSModulePath to Python.
    # Windows PowerShell 5.1 cannot load those modules; use its own built-ins.
    child_env = {key: value for key, value in os.environ.items()
                 if key.upper() != "PSMODULEPATH"}
    child_env["PSModulePath"] = str(Path(os.environ["SystemRoot"]) /
                                   "System32/WindowsPowerShell/v1.0/Modules")
    child_env["PERIMETER_CAPTURE_PRIVATE_DIR"] = str(path)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=30,
    )
    if result.returncode:
        raise TransferError("Cannot protect export directory")
    return path


def atomic_private_json(path, value):
    path = Path(path)
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as stream:
            temp = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def cmd_path(path):
    value = str(Path(path).absolute())
    if any(char in value for char in '%!^"\r\n'):
        raise TransferError("Unsupported character in capture path")
    return '"' + value + '"'


def capture_config(config, private_dir):
    if os.name != "nt":
        raise TransferError("Capture requires native Windows Python")
    config = Path(config).absolute()
    if config.name.lower() not in {"config_v3.cmd", "kpp_env_config_final.cmd"}:
        raise TransferError("Select a production configuration file, not a launcher")
    if not config.is_file():
        raise TransferError("Configuration file is absent")
    child_env = {key: value for key, value in os.environ.items()
                 if not (key.upper().startswith(PREFIXES + ("SQL_",))
                         or key.upper() in ALIASES)}
    # The examples refer to ROOT/PROJECT_ROOT; the local config can override them.
    child_env.update(ROOT=str(config.parent.parent), PROJECT_ROOT=str(config.parent.parent))
    with tempfile.TemporaryDirectory(prefix="capture-", dir=private_dir) as work:
        work = Path(work)
        record = work / "capture.local.json"
        wrapper = work / "capture.cmd"
        script = Path(__file__).absolute()
        text = (
            "@echo off\nsetlocal EnableExtensions DisableDelayedExpansion\n"
            "chcp 65001 >nul\n"
            f"call {cmd_path(config)} >nul 2>nul\n"
            "if errorlevel 1 exit /b 2\n"
            "setlocal DisableDelayedExpansion\n"
            f"{cmd_path(sys.executable)} {cmd_path(script)} _capture "
            f"--output {cmd_path(record)}\n"
            "exit /b %errorlevel%\n"
        )
        wrapper.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        # Supply CMD's command string directly: Windows quoting rules differ
        # from subprocess.list2cmdline's C-runtime argv quoting.
        command = 'cmd.exe /d /q /v:off /s /c "' + cmd_path(wrapper) + '"'
        result = subprocess.run(
            command, cwd=config.parent.parent, env=child_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
        if result.returncode or not record.is_file():
            raise TransferError("Local CMD configuration capture failed")
        data = json.loads(record.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise TransferError("Invalid captured environment")
        return normalized_environment(data)


def export_bundle(config, output):
    output = Path(output).absolute()
    if not output.name.endswith(".local.json"):
        raise TransferError("Export filename must end with .local.json")
    private_dir = private_directory(output.parent)
    token = secrets.token_hex(32)
    if output.exists():
        old = json.loads(output.read_text(encoding="utf-8"))
        if (not isinstance(old, dict) or old.get("version") != 1
                or not re.fullmatch(r"[0-9a-f]{64}", str(old.get("ha_token", "")))):
            raise TransferError("Existing bundle is invalid; it was preserved")
        token = old["ha_token"]
    env = capture_config(config, private_dir)
    missing = missing_fields(env)
    if missing:
        raise TransferError("Missing settings: " + ", ".join(missing))
    bundle = {
        "version": 1,
        "source_config": str(Path(config).absolute()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ha_token": token,
        "environment": env,
    }
    atomic_private_json(output, bundle)
    return {"exported": True, "file": str(output), "variables": len(env),
            "web_auth_present": bool(env.get("KPP_WEB_AUTH_USER") and
                                     env.get("KPP_WEB_AUTH_PASSWORD"))}


def profile_reports(root, private_dir):
    captured = {}
    reports = {}
    for name, relative in PROFILES.items():
        try:
            env = capture_config(Path(root) / relative, private_dir)
            captured[name] = env
            reports[name] = {
                "captured": True, "variables": len(env),
                "missing": missing_fields(env),
                "web_auth_present": bool(env.get("KPP_WEB_AUTH_USER") and
                                         env.get("KPP_WEB_AUTH_PASSWORD")),
            }
        except Exception as exc:
            reports[name] = {"captured": False, "error": type(exc).__name__}
    different = []
    if len(captured) == 2:
        first, second = captured.values()
        different = sorted(key for key in set(first) | set(second)
                           if first.get(key) != second.get(key))
    return {"profiles": reports, "different_variables": different}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    profiles = commands.add_parser("profiles", help="Print field names/status only")
    profiles.add_argument("--root", type=Path, required=True)
    profiles.add_argument("--private-dir", type=Path, required=True)
    export = commands.add_parser("export", help="Create a private transfer bundle")
    export.add_argument("--config", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    capture = commands.add_parser("_capture", help=argparse.SUPPRESS)
    capture.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "_capture":
            atomic_private_json(args.output, selected_environment(os.environ))
            return 0
        if args.command == "profiles":
            directory = private_directory(args.private_dir)
            report = profile_reports(args.root, directory)
        else:
            report = export_bundle(args.config, args.output)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # CMD/OS exceptions can contain command text or credential values.
        error = str(exc) if isinstance(exc, TransferError) else type(exc).__name__
        print(json.dumps({"error": error}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
