@echo off
setlocal EnableExtensions DisableDelayedExpansion
title 2 RusGuard Sync v3.4.5 + Observability
cd /d "%~dp0.."
set "ROOT=%CD%"

if not exist "%ROOT%\deploy\config_v3.cmd" goto :missing_config
call "%ROOT%\deploy\config_v3.cmd"
if errorlevel 1 goto :fatal

if not defined PY64 call "%ROOT%\deploy\resolve_python.cmd"
if errorlevel 1 goto :fatal
if not defined PY64 goto :missing_python
if not exist "%PY64%" goto :bad_python
if not exist "%ROOT%\DB_RusGard\db_sync_v2.py" goto :bad_script
if not exist "%ROOT%\deploy\run_service.py" goto :bad_runner
if not exist "%ROOT%\deploy\monitored_rusguard.py" goto :bad_monitor

cd /d "%ROOT%\DB_RusGard"
if errorlevel 1 goto :bad_workdir

:restart
set "PERIMETER_RELEASE=3.4.5-warehouse-recheck+obs2"
"%PY64%" -u "%ROOT%\deploy\run_service.py" --service "Perimeter.RusGuardSync" --script "%ROOT%\deploy\monitored_rusguard.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" "%PY64%" "%ROOT%\deploy\report_service_exit.py" --service "Perimeter.RusGuardSync" --exit-code %RC% >nul 2>&1
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
echo [FATAL] Script does not exist: %ROOT%\DB_RusGard\db_sync_v2.py
goto :fatal

:bad_runner
echo [FATAL] Missing deploy\run_service.py
goto :fatal

:bad_monitor
echo [FATAL] Missing deploy\monitored_rusguard.py
goto :fatal

:bad_workdir
echo [FATAL] Cannot enter work directory: %ROOT%\DB_RusGard
goto :fatal

:fatal
echo.
echo This component was not started.
pause
exit /b 1
