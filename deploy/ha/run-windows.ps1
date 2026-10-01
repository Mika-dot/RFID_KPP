param([string]$Config='D:\PerimeterHA\node.json', [string]$ProductionRoot='D:\Desktop\RFID_KPP-main')
$ErrorActionPreference='Stop'
$env:PERIMETER_HA_CONFIG=$Config
$env:PERIMETER_HA_PRODUCTION_ROOT=$ProductionRoot
& cmd.exe /c ('"{0}\run-windows.cmd"' -f $PSScriptRoot)
exit $LASTEXITCODE
