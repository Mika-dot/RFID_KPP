@echo off
setlocal EnableExtensions DisableDelayedExpansion
if not defined PERIMETER_HA_CONFIG set "PERIMETER_HA_CONFIG=D:\PerimeterHA\node.json"
if not defined PERIMETER_HA_BUNDLE set "PERIMETER_HA_BUNDLE=D:\PerimeterHA\transfer-private\environment.local.json"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-windows.ps1" -Config "%PERIMETER_HA_CONFIG%" -Bundle "%PERIMETER_HA_BUNDLE%"
exit /b %errorlevel%
