# Send only the existing HA token to the existing monitoring server over SSH.
# No token in command arguments, output, clipboard or the monitoring dashboard.
$ErrorActionPreference = 'Stop'
try {
    $Bundle = Get-Content -Raw -LiteralPath 'D:\PerimeterHA\transfer-private\environment.local.json' | ConvertFrom-Json
    $Token = [string]$Bundle.ha_token
    if ($Token -notmatch '^[0-9a-f]{64}$') {
        Write-Host 'Existing HA token is missing or invalid; transfer cancelled.' -ForegroundColor Yellow
        exit 1
    }
    $Remote = 'set -eu; umask 077; test ! -L /home/mkm/.perimeter-ha; mkdir -p /home/mkm/.perimeter-ha; chmod 700 /home/mkm/.perimeter-ha; test ! -L /home/mkm/.perimeter-ha/observer.token; cat > /home/mkm/.perimeter-ha/observer.token'
    $Token | & ssh.exe -T -o ConnectTimeout=15 mkm@172.31.0.97 $Remote
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'SSH transfer failed; monitoring installation has not started.' -ForegroundColor Yellow
        exit 1
    }
    Write-Host 'HA_OBSERVER_TOKEN_TRANSFERRED_WITHOUT_OUTPUT' -ForegroundColor Green
}
catch {
    Write-Host 'Token transfer could not complete. No secret values were printed.' -ForegroundColor Yellow
    exit 1
}
finally {
    $Token = $null
    $Bundle = $null
}
