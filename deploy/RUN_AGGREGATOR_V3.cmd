@echo off
setlocal EnableExtensions DisableDelayedExpansion
title 4 KPP Aggregator FINAL FIXED v3.4.1 Warehouse
cd /d "%~dp0.."
set "ROOT=%CD%"

if not exist "%ROOT%\deploy\config_v3.cmd" goto :missing_config
call "%ROOT%\deploy\config_v3.cmd"
if errorlevel 1 goto :fatal

if not defined PY64 call "%ROOT%\deploy\resolve_python.cmd"
if errorlevel 1 goto :fatal
if not defined PY64 goto :missing_python
if not exist "%PY64%" goto :bad_python
if not exist "%ROOT%\KPP\kpp_aggregator_v3_warehouse.py" goto :bad_script

cd /d "%ROOT%\KPP"
if errorlevel 1 goto :bad_workdir

:restart
"%PY64%" -u "%ROOT%\KPP\kpp_aggregator_v3_warehouse.py"
set "RC=%ERRORLEVEL%"
echo.
echo [%DATE% %TIME%] Exit code %RC%. Restarting in 5 seconds.
timeout /t 5 /nobreak >nul
goto :restart

:missing_config
echo [FATAL] Missing deploy\config_v3.cmd
goto :fatal

:missing_python
echo [FATAL] PY64 is empty after Python detection.
goto :fatal

:bad_python
echo [FATAL] Python file does not exist: %PY64%
goto :fatal

:bad_script
echo [FATAL] Script does not exist: %ROOT%\KPP\kpp_aggregator_v3_warehouse.py
goto :fatal

:bad_workdir
echo [FATAL] Cannot enter work directory: %ROOT%\KPP
goto :fatal

:fatal
echo.
echo This component was not started.
pause
exit /b 1
