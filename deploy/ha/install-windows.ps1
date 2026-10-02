param(
    [string]$Root='D:\Desktop\RFID_KPP-main',
    [string]$Config='D:\PerimeterHA\node.json',
    [string]$Bundle='D:\PerimeterHA\transfer-private\environment.local.json'
)
$ErrorActionPreference='Stop'
$sourceRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if (-not (Test-Path -LiteralPath "$sourceRoot\guardian\boot.py")) { throw 'HA source checkout is absent' }
if (-not (Test-Path -LiteralPath "$sourceRoot\.git")) { throw 'Use a separate Git checkout for HA installation' }
if (-not (Test-Path -LiteralPath $Config)) { throw 'Run windows_tool.py prepare first' }
if (-not (Test-Path -LiteralPath $Bundle)) { throw 'Private environment bundle is absent' }
$node=Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
if ($node.node_id -ne 'physical' -or $node.controller_enabled -ne $false) { throw 'Invalid physical node identity' }
if ([IO.Path]::GetFullPath($node.root) -ne $sourceRoot) { throw 'Config root must match the installer checkout' }
if ([IO.Path]::GetFullPath($Root) -eq $sourceRoot) { throw 'Keep the production directory separate' }
if (-not (Test-Path -LiteralPath $node.python)) { throw 'Configured Python is absent' }
# Check the existing environment; installation does not modify running production packages.
& $node.python -c 'import psutil; assert 6 <= int(psutil.__version__.split(chr(46))[0]) < 8'
if ($LASTEXITCODE -ne 0) { throw 'Install psutil>=6,<8 in the configured Python environment first' }
$task=Get-ScheduledTask -TaskName 'PerimeterGuardian' -ErrorAction SilentlyContinue
if ($task -and $task.State -eq 'Running') { throw 'An existing guardian task is running; registration was preserved' }
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}\deploy\ha\run-windows.ps1" -Config "{1}" -Bundle "{2}"' -f $sourceRoot,$Config,$Bundle) -WorkingDirectory $sourceRoot
$trigger=New-ScheduledTaskTrigger -AtStartup
$settings=New-ScheduledTaskSettingsSet -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName 'PerimeterGuardian' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
if (-not (Get-NetFirewallRule -DisplayName 'Perimeter HA node agent' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'Perimeter HA node agent' -Direction Inbound -Protocol TCP -LocalPort 18200 -Action Allow -RemoteAddress '172.31.0.134','172.31.0.192' | Out-Null
}
Write-Host 'Registered PerimeterGuardian. Task was not started; production processes were not changed.'
