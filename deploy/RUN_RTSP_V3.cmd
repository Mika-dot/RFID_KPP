@echo off
setlocal EnableExtensions DisableDelayedExpansion
title 3 RTSP YOLO FINAL FIXED v3.4
cd /d "%~dp0.."
set "ROOT=%CD%"

if not exist "%ROOT%\deploy\config_v3.cmd" goto :missing_config
call "%ROOT%\deploy\config_v3.cmd"
if errorlevel 1 goto :fatal

if not defined PY64 call "%ROOT%\deploy\resolve_python.cmd"
if errorlevel 1 goto :fatal
if not defined PY64 goto :missing_python
if not exist "%PY64%" goto :bad_python
if not exist "%ROOT%\RTSP\RTSP_yolo_DB_v3.py" goto :bad_script

cd /d "%ROOT%\RTSP"
if errorlevel 1 goto :bad_workdir

:restart
"%PY64%" -u "%ROOT%\RTSP\RTSP_yolo_DB_v3.py"
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
echo [FATAL] Script does not exist: %ROOT%\RTSP\RTSP_yolo_DB_v3.py
goto :fatal

:bad_workdir
echo [FATAL] Cannot enter work directory: %ROOT%\RTSP
goto :fatal

:fatal
echo.
echo This component was not started.
pause
exit /b 1
