param([string]$Root='D:\Desktop\RFID_KPP-main', [string]$Config='D:\PerimeterHA\node.json')
$ErrorActionPreference='Stop'
if (-not (Test-Path "$Root\guardian\boot.py")) { throw 'HA branch is not installed' }
if (-not (Test-Path $Config)) { throw 'Fill the local node config first' }
if (-not (Test-Path 'D:\PerimeterHA\secrets.local.cmd')) { throw 'Fill local HA secrets first' }
& "$Root\venv64\Scripts\python.exe" -m pip install 'psutil>=6,<8'
if ($LASTEXITCODE -ne 0) { throw 'psutil install failed' }
$sourceRoot=$Root
if (-not (Test-Path "$Root\.git")) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Install Git for Windows for automatic main updates' }
    $sourceRoot='D:\PerimeterHA\source'
    if (-not (Test-Path "$sourceRoot\.git")) {
        & git clone --branch feature/perimeter-ha-guardian https://github.com/Mika-dot/RFID_KPP.git $sourceRoot
        if ($LASTEXITCODE -ne 0) { throw 'HA source clone failed' }
    }
    $node=Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
    $node.root=$sourceRoot
    $node.update_source=$sourceRoot
    $node | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Config -Encoding UTF8
}
# Old RUN_RFID_KPP_FINAL.cmd / ProcessHost / login autorun MUST be disabled before cutover.
# This script registers the agent but does not start or stop production processes.
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}\deploy\ha\run-windows.ps1" -Config "{1}" -ProductionRoot "{2}"' -f $sourceRoot,$Config,$Root) -WorkingDirectory $sourceRoot
$trigger=New-ScheduledTaskTrigger -AtStartup
$settings=New-ScheduledTaskSettingsSet -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName 'PerimeterGuardian' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
New-NetFirewallRule -DisplayName 'Perimeter HA node agent' -Direction Inbound -Protocol TCP -LocalPort 18200 -Action Allow -RemoteAddress LocalSubnet -ErrorAction SilentlyContinue | Out-Null
Write-Host 'Registered PerimeterGuardian. Complete doctor and SQL cutover before Start-ScheduledTask.'
