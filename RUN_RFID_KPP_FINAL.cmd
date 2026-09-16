@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "ROOT=%CD%"
set "PATCH_VERSION=3.4.5-warehouse-recheck"

echo ================================================================
echo RFID KPP FINAL FIXED %PATCH_VERSION%
echo Root: %ROOT%
echo ================================================================

if not exist "%ROOT%\deploy\config_v3.cmd" goto :missing_patch
call "%ROOT%\deploy\config_v3.cmd"
if errorlevel 1 goto :fatal
if not exist "%ROOT%\runtime" mkdir "%ROOT%\runtime"

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\deploy\stop_kpp_processes.ps1" >nul 2>&1
timeout /t 2 /nobreak >nul

call "%ROOT%\deploy\resolve_python.cmd"
if errorlevel 1 goto :fatal

if not defined PY64 goto :missing_py64
if not defined PY32 goto :missing_py32

echo [OK] Python 64-bit: %PY64%
echo [OK] Python 32-bit: %PY32%

call "%ROOT%\deploy\ensure_dependencies.cmd"
if errorlevel 1 goto :fatal

"%PY64%" "%ROOT%\deploy\apply_migration_v3.py"
if errorlevel 1 goto :fatal

"%PY64%" "%ROOT%\deploy\precheck_v3.py"
if errorlevel 1 goto :fatal

"%PY64%" "%ROOT%\deploy\start_services_v3.py"
if errorlevel 1 goto :fatal

echo [OK] All v3.4.5 services were launched.
echo [INFO] Waiting for web service: http://127.0.0.1:5050
"%PY64%" "%ROOT%\deploy\wait_for_web_v3.py" --url "http://127.0.0.1:5050" --timeout 90 --open
if errorlevel 1 echo [WARN] Web did not answer within 90 seconds. Check service windows.
exit /b 0

:missing_patch
echo [FATAL] deploy\config_v3.cmd is missing.
echo Extract this patch into the project root with overwrite enabled.
goto :fatal

:missing_py64
echo [FATAL] PY64 is empty after Python detection.
goto :fatal

:missing_py32
echo [FATAL] PY32 is empty after Python detection.
goto :fatal

:fatal
echo.
echo [FATAL] RFID KPP was not started. Read the error above.
pause
exit /b 1
