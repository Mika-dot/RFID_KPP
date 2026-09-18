@echo off
setlocal EnableExtensions DisableDelayedExpansion
title 1 RFID Reader v3.4.6 Business Flow Self-Heal
cd /d "%~dp0.."
set "ROOT=%CD%"

if not exist "%ROOT%\deploy\config_v3.cmd" goto :missing_config
call "%ROOT%\deploy\config_v3.cmd"
if errorlevel 1 goto :fatal

if not defined PY32 call "%ROOT%\deploy\resolve_python.cmd"
if errorlevel 1 goto :fatal
if not defined PY32 goto :missing_python
if not exist "%PY32%" goto :bad_python
if not exist "%ROOT%\RFID_reader_v4\rfid_to_sql_v4.py" goto :bad_script
if not exist "%ROOT%\deploy\run_service.py" goto :bad_runner
if not exist "%ROOT%\deploy\monitored_rfid.py" goto :bad_monitor

cd /d "%ROOT%\RFID_reader_v4"
if errorlevel 1 goto :bad_workdir

:restart
set "PERIMETER_RELEASE=3.4.6-business-flow-selfheal"
"%PY32%" -u "%ROOT%\deploy\run_service.py" --service "Perimeter.RfidReader" --script "%ROOT%\deploy\monitored_rfid.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" "%PY32%" "%ROOT%\deploy\report_service_exit.py" --service "Perimeter.RfidReader" --exit-code %RC% >nul 2>&1
echo.
echo [%DATE% %TIME%] Exit code %RC%. Restarting in 5 seconds.
timeout /t 5 /nobreak >nul
goto :restart

:missing_config
echo [FATAL] Missing deploy\config_v3.cmd
goto :fatal

:missing_python
echo [FATAL] PY32 is empty after Python detection.
goto :fatal

:bad_python
echo [FATAL] Python file does not exist: %PY32%
goto :fatal

:bad_script
echo [FATAL] Script does not exist: %ROOT%\RFID_reader_v4\rfid_to_sql_v4.py
goto :fatal

:bad_runner
echo [FATAL] Missing deploy\run_service.py
goto :fatal

:bad_monitor
echo [FATAL] Missing deploy\monitored_rfid.py
goto :fatal

:bad_workdir
echo [FATAL] Cannot enter work directory: %ROOT%\RFID_reader_v4
goto :fatal

:fatal
echo.
echo This component was not started.
pause
exit /b 1
