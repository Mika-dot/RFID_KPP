@echo off
setlocal EnableExtensions DisableDelayedExpansion
if not defined PERIMETER_HA_CONFIG set "PERIMETER_HA_CONFIG=D:\PerimeterHA\node.json"
if not defined PERIMETER_HA_SECRETS set "PERIMETER_HA_SECRETS=D:\PerimeterHA\secrets.local.cmd"
for %%I in ("%~dp0..\..") do set "HA_ROOT=%%~fI"
cd /d "%HA_ROOT%"
call "deploy\config_v3.cmd"
if errorlevel 1 exit /b 2
call "%PERIMETER_HA_SECRETS%"
if errorlevel 1 exit /b 2
set "PYTHON=%HA_ROOT%\venv64\Scripts\python.exe"
:run
"%PYTHON%" "guardian\boot.py" --config "%PERIMETER_HA_CONFIG%"
powershell -NoProfile -Command "Start-Sleep -Seconds 3"
goto :run
