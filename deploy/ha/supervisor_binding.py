"""Journalled native bootstrap binding; never edit an installed Git checkout."""
from pathlib import Path
import os

DROPIN = Path('/etc/systemd/system/perimeter-guardian.service.d/90-perimeter-bootstrap.conf')


def psquote(value):
    return "'" + str(value).replace("'", "''") + "'"


def systemd_quote(value):
    value = str(value)
    if any(c in value for c in ('\n', '\r', '\0')):
        raise ValueError('InvalidSupervisorArgument')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def capture(run):
    if os.name == 'nt':
        xml = run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                   "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                   "Export-ScheduledTask -TaskName 'PerimeterGuardian' -ErrorAction Stop"], 20)
        if '<Task' not in xml or len(xml) > 256*1024:
            raise RuntimeError('InvalidSupervisorTaskExport')
        return {'kind': 'windows-task', 'xml': xml}
    if DROPIN.is_symlink():
        raise RuntimeError('SupervisorDropinMustNotBeSymlink')
    return {'kind': 'systemd-dropin',
            'previous': DROPIN.read_text(encoding='utf-8') if DROPIN.exists() else None}


def install(saved, args, cfg, candidate, run):
    candidate = Path(candidate).resolve()
    if not (candidate / 'guardian/boot.py').is_file():
        raise RuntimeError('QualifiedBootstrapMissing')
    if saved['kind'] == 'windows-task':
        launcher = candidate / 'deploy/ha/run-windows.ps1'
        values = [str(launcher), str(args.config.resolve()), str(args.environment.resolve())]
        if any(any(c in v for c in ('"', '\r', '\n', '\0')) for v in values):
            raise ValueError('InvalidSupervisorArgument')
        arguments = '-NoProfile -ExecutionPolicy Bypass -File "{}" -Config "{}" -Bundle "{}"'.format(*values)
        script = ("$ErrorActionPreference='Stop'; "
                  "$a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
                  + psquote(arguments) + ' -WorkingDirectory ' + psquote(candidate) + '; '
                  "Set-ScheduledTask -TaskName 'PerimeterGuardian' -Action $a | Out-Null; "
                  "$t=Get-ScheduledTask -TaskName 'PerimeterGuardian'; "
                  "if ($t.Actions.Count -ne 1 -or $t.Actions[0].Arguments -ne "
                  + psquote(arguments) + ") { throw 'SupervisorBindingNotConfirmed' }")
        run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], 30)
    elif saved['kind'] == 'systemd-dropin':
        DROPIN.parent.mkdir(parents=True, exist_ok=True)
        command = ' '.join(systemd_quote(v) for v in (
            cfg['python'], candidate / 'guardian/boot.py', '--config', args.config.resolve()))
        # Record is durable before this is called; restore uses the same path.
        DROPIN.write_text('[Service]\nExecStart=\nExecStart='+command+'\n', encoding='utf-8')
        DROPIN.chmod(0o644)
        run(['systemctl', 'daemon-reload'], 30)
        effective = run(['systemctl', 'show', 'perimeter-guardian.service', '-p', 'ExecStart', '--value'], 20)
        if str(candidate / 'guardian/boot.py') not in effective:
            raise RuntimeError('SupervisorBindingNotConfirmed')
    else:
        raise RuntimeError('UnknownSupervisorBinding')


def restore(saved, args, run):
    if saved['kind'] == 'windows-task':
        # Avoid embedding private task XML or long payloads in process argv.
        journal = Path(args.config).parent / 'supervisor-task-restore.xml'
        fd = os.open(str(journal), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(saved['xml'])
        script = ("$ErrorActionPreference='Stop'; Register-ScheduledTask "
                  "-TaskName 'PerimeterGuardian' -Xml (Get-Content -LiteralPath "
                  + psquote(journal) + ' -Raw -Encoding UTF8) -Force | Out-Null')
        try:
            run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], 30)
        finally:
            journal.unlink(missing_ok=True)
    elif saved['kind'] == 'systemd-dropin':
        if saved['previous'] is None:
            DROPIN.unlink(missing_ok=True)
        else:
            DROPIN.parent.mkdir(parents=True, exist_ok=True)
            DROPIN.write_text(saved['previous'], encoding='utf-8')
            DROPIN.chmod(0o644)
        run(['systemctl', 'daemon-reload'], 30)
    else:
        raise RuntimeError('UnknownSupervisorBinding')
