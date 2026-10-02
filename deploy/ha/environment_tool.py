"""Capture the physical node's local CMD settings without printing credentials.

Run with native Windows Python. Exported *.local.json files stay outside Git.
The CMD config runs in a child process; the caller's environment is unchanged.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


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


def export_bundle(config, output, web_auth=False):
    output = Path(output).absolute()
    if not output.name.endswith(".local.json"):
        raise TransferError("Export filename must end with .local.json")
    private_dir = private_directory(output.parent)
    token = secrets.token_hex(32)
    old_env = {}
    if output.exists():
        old = json.loads(output.read_text(encoding="utf-8"))
        if (not isinstance(old, dict) or old.get("version") != 1
                or not re.fullmatch(r"[0-9a-f]{64}", str(old.get("ha_token", "")))):
            raise TransferError("Existing bundle is invalid; it was preserved")
        token = old["ha_token"]
        old_env = old.get("environment", {})
        if not isinstance(old_env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in old_env.items()
        ):
            raise TransferError("Existing bundle environment is invalid; it was preserved")
    env = capture_config(config, private_dir)
    missing = missing_fields(env)
    if missing:
        raise TransferError("Missing settings: " + ", ".join(missing))
    for key in ("KPP_WEB_AUTH_USER", "KPP_WEB_AUTH_PASSWORD"):
        if not env.get(key) and isinstance(old_env, dict) and old_env.get(key):
            env[key] = old_env[key]
    if web_auth:
        if not env.get("KPP_WEB_AUTH_USER"):
            env["KPP_WEB_AUTH_USER"] = input("Web login [perimeter]: ").strip() or "perimeter"
        if not env.get("KPP_WEB_AUTH_PASSWORD"):
            password = getpass.getpass("Web password: ")
            confirm = getpass.getpass("Repeat web password: ")
            if not password or password != confirm:
                raise TransferError("Web passwords are empty or do not match")
            env["KPP_WEB_AUTH_PASSWORD"] = password
        env["KPP_WEB_AUTH_REQUIRED"] = "1"
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


def linux_odbc(connection):
    """Replace only the real DRIVER field, preserving braced passwords verbatim."""
    if not isinstance(connection, str) or any(c in connection for c in "\r\n\0"):
        raise TransferError("Invalid ODBC connection")
    index = 0
    spans = []
    while index < len(connection):
        while index < len(connection) and connection[index] in "; \t":
            index += 1
        if index == len(connection):
            break
        equal = connection.find("=", index)
        if equal < 0 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_ ]*", connection[index:equal].strip()):
            raise TransferError("Invalid ODBC field syntax")
        key = connection[index:equal].strip().upper()
        start = equal + 1
        while start < len(connection) and connection[start] in " \t":
            start += 1
        end = start
        if start < len(connection) and connection[start] == "{":
            end += 1
            while end < len(connection):
                if connection[end] == "}":
                    if end + 1 < len(connection) and connection[end + 1] == "}":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            else:
                raise TransferError("Unclosed ODBC field")
            next_field = end
            while next_field < len(connection) and connection[next_field] in " \t":
                next_field += 1
            if next_field < len(connection) and connection[next_field] != ";":
                raise TransferError("Invalid ODBC field boundary")
        else:
            separator = connection.find(";", start)
            next_field = len(connection) if separator < 0 else separator
            end = next_field
            while end > start and connection[end - 1] in " \t":
                end -= 1
        if key == "DRIVER":
            spans.append((start, end))
        index = next_field + 1
    if len(spans) != 1:
        raise TransferError("Exactly one explicit ODBC DRIVER field is required")
    start, end = spans[0]
    return connection[:start] + "{ODBC Driver 18 for SQL Server}" + connection[end:]


def linux_environment(bundle, cfg):
    if (not isinstance(bundle, dict) or bundle.get("version") != 1
            or not re.fullmatch(r"[0-9a-f]{64}", str(bundle.get("ha_token", "")))):
        raise TransferError("Invalid transfer bundle")
    source = bundle.get("environment")
    if not isinstance(source, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in source.items()
    ):
        raise TransferError("Invalid environment in bundle")
    if cfg.get("node_id") not in ("perimetr", "comparator"):
        raise TransferError("Linux import requires a VM node_id")
    root = PurePosixPath(cfg.get("root", ""))
    if not root.is_absolute():
        raise TransferError("Configure an absolute Linux source root")
    env = normalized_environment(source)
    env.pop("KPP_MIGRATION_TRUSTED_CONN", None)  # Windows trusted login is not copied.
    for key in ("RFID_DB_CONNECTION", "KPP_CONN_STR", "KPP_WEB_DB_CONNECTION",
                "KPP_TASK_CONN_STR", "COMMON_DB_CONN", "SQL_CONN"):
        if env.get(key):
            env[key] = linux_odbc(env[key])
    env["SRC_DRIVER"] = env["DST_DRIVER"] = "ODBC Driver 18 for SQL Server"
    non_reel = env.get("KPP_NON_REEL_TAGS_FILE", "")
    if re.match(r"^[A-Za-z]:[\\/]", non_reel):
        if re.split(r"[\\/]", non_reel)[-1].lower() != "non_reel_tags.txt":
            raise TransferError("Configure the custom KPP_NON_REEL_TAGS_FILE on Linux")
        env["KPP_NON_REEL_TAGS_FILE"] = str(root / "deploy/non_reel_tags.txt")
    env.update(cfg.get("env", {}))
    # Explicitly retain the prepared native bridge/device settings on every VM.
    if env.get("RFID_SDK_MODE") != "wine" or env.get("RFID_YOLO_DEVICE") != "cpu":
        raise TransferError("Configure Wine and the CPU device in the VM JSON")
    for key, value in env.items():
        if key.endswith(("_PATH", "_FILE", "_DIR")) and isinstance(value, str):
            if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", value):
                raise TransferError("Configure Linux path for " + key)
    missing = missing_fields(env)
    missing += [key for key in ("KPP_WEB_AUTH_USER", "KPP_WEB_AUTH_PASSWORD") if not env.get(key)]
    if missing:
        raise TransferError("Missing settings: " + ", ".join(missing))
    env["KPP_WEB_AUTH_REQUIRED"] = "1"
    env["PERIMETER_HA_TOKEN"] = bundle["ha_token"]
    env["PERIMETER_HA_SQL"] = env["KPP_CONN_STR"]
    return env


def environment_text(env):
    rows = ["# Generated locally for systemd EnvironmentFile; do not source in Bash."]
    for key, value in sorted(env.items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not isinstance(value, str):
            raise TransferError("Invalid environment field")
        if any(c in value for c in "\r\n\0\ufeff"):
            raise TransferError("Unsupported control character in " + key)
        escaped = "".join("\\" + c if c in '\\"$`' else c for c in value)
        rows.append(key + '="' + escaped + '"')
    return "\n".join(rows) + "\n"


def read_generated_environment(path):
    """Read exactly our quoted format without shell execution or expansion."""
    env = {}
    for row in Path(path).read_text(encoding="utf-8").splitlines():
        if not row or row.startswith("#"):
            continue
        key, separator, raw = row.partition("=")
        if (not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                or len(raw) < 2 or raw[0] != '"' or raw[-1] != '"'):
            raise TransferError("EnvironmentFile was not generated by this tool")
        chars = []
        index = 1
        while index < len(raw) - 1:
            char = raw[index]
            if char == "\\":
                index += 1
                if index >= len(raw) - 1 or raw[index] not in '\\"$`':
                    raise TransferError("Invalid EnvironmentFile escape")
                char = raw[index]
            elif char == '"':
                raise TransferError("Invalid EnvironmentFile quote")
            chars.append(char)
            index += 1
        env[key] = "".join(chars)
    return env


def atomic_environment(path, text, uid, gid):
    path = Path(path)
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as stream:
            temp = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temp.chmod(0o600)
        os.chown(temp, uid, gid)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def install_bundle(bundle_path, node_path, environment_path):
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        raise TransferError("Linux installation requires sudo")
    import pwd
    active = subprocess.run(["systemctl", "is-active", "--quiet", "perimeter-guardian"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if active.returncode == 0:
        raise TransferError("Stop the VM guardian before importing configuration")
    cfg = json.loads(Path(node_path).read_text(encoding="utf-8-sig"))
    bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    env = linux_environment(bundle, cfg)
    text = environment_text(env)
    user = pwd.getpwnam("perimeter")
    destination = Path(environment_path)
    backup = destination.with_name(destination.name + ".pre-import")
    if destination.exists() and not backup.exists():
        atomic_environment(backup, destination.read_text(encoding="utf-8"), user.pw_uid, user.pw_gid)
    atomic_environment(destination, text, user.pw_uid, user.pw_gid)
    return {"installed": True, "node_id": cfg["node_id"], "variables": len(env),
            "web_auth_present": True, "service_started": False}


def run_doctor(node_path, environment_path):
    cfg = json.loads(Path(node_path).read_text(encoding="utf-8-sig"))
    env = os.environ.copy()
    env.update(read_generated_environment(environment_path))
    result = subprocess.run([cfg["python"], "-m", "guardian", "--config", str(node_path), "doctor"],
                            cwd=cfg["root"], env=env)
    return result.returncode


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
    export.add_argument("--web-auth", action="store_true", help="Prompt for missing web credentials locally")
    install = commands.add_parser("install", help="Install the transferred bundle on a stopped Ubuntu VM")
    install.add_argument("--bundle", type=Path, required=True)
    install.add_argument("--node", type=Path, default=Path("/etc/perimeter/node.json"))
    install.add_argument("--environment", type=Path, default=Path("/etc/perimeter/environment"))
    doctor = commands.add_parser("doctor", help="Run the native doctor with the generated EnvironmentFile")
    doctor.add_argument("--node", type=Path, default=Path("/etc/perimeter/node.json"))
    doctor.add_argument("--environment", type=Path, default=Path("/etc/perimeter/environment"))
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
        elif args.command == "export":
            report = export_bundle(args.config, args.output, args.web_auth)
        elif args.command == "install":
            report = install_bundle(args.bundle, args.node, args.environment)
        else:
            return run_doctor(args.node, args.environment)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # CMD/OS exceptions can contain command text or credential values.
        error = str(exc) if isinstance(exc, TransferError) else type(exc).__name__
        print(json.dumps({"error": error}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
