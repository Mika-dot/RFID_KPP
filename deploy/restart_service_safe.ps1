param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("RfidReader", "RusGuardSync", "Yolo", "Aggregator", "WebDashboard")]
    [string]$Service,

    [int]$TimeoutSec = 45
)

$ErrorActionPreference = "Stop"

$map = @{
    RfidReader   = @{ Name = "Perimeter.RfidReader";   Port = 18101 }
    RusGuardSync = @{ Name = "Perimeter.RusGuardSync"; Port = 18102 }
    Yolo         = @{ Name = "Perimeter.Yolo";         Port = 18103 }
    Aggregator   = @{ Name = "Perimeter.Aggregator";   Port = 18104 }
    WebDashboard = @{ Name = "Perimeter.WebDashboard"; Port = 18105 }
}

$spec = $map[$Service]
$serviceName = [string]$spec.Name
$port = [int]$spec.Port

function Get-ServiceRunnerProcess {
    param([string]$ExactServiceName)

    # Deliberately match ONLY the Python run_service child. Never kill cmd.exe,
    # powershell.exe, wrappers, console hosts, or sibling Perimeter services.
    @(
        Get-CimInstance Win32_Process | Where-Object {
            $_.Name -match '^python(w)?\.exe$' -and
            $_.CommandLine -and
            $_.CommandLine -match '(?i)run_service\.py' -and
            $_.CommandLine -match ('(?i)--service\s+["'']?' + [regex]::Escape($ExactServiceName) + '["'']?(\s|$)')
        }
    )
}

$before = Get-ServiceRunnerProcess -ExactServiceName $serviceName
if ($before.Count -ne 1) {
    throw "SAFE ABORT: expected exactly one Python runner for $serviceName, found $($before.Count). Nothing was stopped."
}

$oldPid = [int]$before[0].ProcessId
Write-Host "Restarting ONLY $serviceName child PID=$oldPid" -ForegroundColor Cyan
Stop-Process -Id $oldPid -Force

$deadline = (Get-Date).AddSeconds([Math]::Max(10, $TimeoutSec))
$newPid = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $after = Get-ServiceRunnerProcess -ExactServiceName $serviceName
    if ($after.Count -eq 1 -and [int]$after[0].ProcessId -ne $oldPid) {
        $newPid = [int]$after[0].ProcessId
        break
    }
    if ($after.Count -gt 1) {
        throw "SAFE ABORT: more than one Python runner appeared for $serviceName. Manual inspection required."
    }
}

if ($null -eq $newPid) {
    throw "Wrapper did not restart $serviceName within $TimeoutSec seconds. No other process was touched."
}

# Liveness is the restart success criterion. Readiness may legitimately remain
# degraded while semantic/business-flow checks settle.
$liveOk = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $body = & curl.exe -s --max-time 2 "http://127.0.0.1:$port/health/live"
    if (-not [string]::IsNullOrWhiteSpace($body)) {
        try {
            $live = $body | ConvertFrom-Json
            if ($live.status -eq "ok") {
                $liveOk = $true
                break
            }
        } catch { }
    }
}

if (-not $liveOk) {
    throw "$serviceName restarted as PID=$newPid but did not become live on port $port. Other services were not touched."
}

Write-Host "[OK] $serviceName restarted safely: $oldPid -> $newPid" -ForegroundColor Green
$readyBody = & curl.exe -s --max-time 3 "http://127.0.0.1:$port/health/ready"
if (-not [string]::IsNullOrWhiteSpace($readyBody)) {
    try {
        $ready = $readyBody | ConvertFrom-Json
        Write-Host "ready=$($ready.status) port=$port"
    } catch {
        Write-Host "ready=unparseable port=$port" -ForegroundColor Yellow
    }
} else {
    Write-Host "ready=no-response port=$port" -ForegroundColor Yellow
}
