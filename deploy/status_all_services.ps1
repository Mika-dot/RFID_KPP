$ErrorActionPreference = "Continue"

$services = @(
    @{ Name = "RfidReader";   Port = 18101 },
    @{ Name = "RusGuardSync"; Port = 18102 },
    @{ Name = "Yolo";         Port = 18103 },
    @{ Name = "Aggregator";   Port = 18104 },
    @{ Name = "WebDashboard"; Port = 18105 }
)

foreach ($svc in $services) {
    $port = [int]$svc.Port
    $name = [string]$svc.Name
    $raw = & curl.exe -s --max-time 3 "http://127.0.0.1:$port/health/ready"
    if ([string]::IsNullOrWhiteSpace($raw)) {
        Write-Host ("{0,-14} {1}  NO RESPONSE" -f $name, $port) -ForegroundColor Red
        continue
    }
    try {
        $h = $raw | ConvertFrom-Json
        $status = [string]$h.status
        $color = if ($status -eq "ok") { "Green" } elseif ($status -eq "degraded") { "Yellow" } else { "Red" }
        Write-Host ("{0,-14} {1}  {2}" -f $name, $port, $status) -ForegroundColor $color
    } catch {
        Write-Host ("{0,-14} {1}  BAD JSON" -f $name, $port) -ForegroundColor Red
    }
}
