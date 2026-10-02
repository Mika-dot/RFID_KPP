param(
    [string]$Config='D:\PerimeterHA\node.json',
    [string]$Bundle='D:\PerimeterHA\transfer-private\environment.local.json'
)
$ErrorActionPreference='Stop'
$node=Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
while ($true) {
    & $node.python (Join-Path $PSScriptRoot 'windows_tool.py') serve --config $Config --bundle $Bundle
    Start-Sleep -Seconds 3
}
