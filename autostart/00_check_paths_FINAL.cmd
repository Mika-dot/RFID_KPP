@echo off
setlocal EnableExtensions
call "%~dp0kpp_env_config_FINAL.cmd"
title RFID KPP FINAL path check

echo ============================================================
echo RFID KPP FINAL path check
echo PROJECT_ROOT=%PROJECT_ROOT%
echo ============================================================
echo.

if exist "%PROJECT_ROOT%" (echo [OK] root) else (echo [BAD] root not found & pause & exit /b 1)
if exist "%PROJECT_ROOT%\RFID_readers" (echo [OK] RFID_readers) else (echo [BAD] RFID_readers not found)
if exist "%PROJECT_ROOT%\KPP\kpp_1_reliable_v2.4_full_rebuild.py" (echo [OK] KPP python) else (echo [BAD] KPP python not found)
if exist "%PROJECT_ROOT%\RTSP\RTSP_yolo_DB_v2.py" (echo [OK] RTSP python) else (echo [BAD] RTSP python not found)
if exist "%PROJECT_ROOT%\DB_RusGard\db_sync.py" (echo [OK] RusGuard python) else (echo [BAD] RusGuard python not found)
if exist "%PROJECT_ROOT%\web\kpp_reel_dashboard_v2.7_ru.py" (echo [OK] Web python) else (echo [BAD] Web python not found)
if exist "%PROJECT_ROOT%\venv64\Scripts\python.exe" (echo [OK] venv64 python) else (echo [WARN] venv64 python not found)

set "RFID_DIR="
for /d %%D in ("%PROJECT_ROOT%\RFID_readers\*") do (
    if exist "%%~fD\UHFAPI.dll" if exist "%%~fD\rfid_to_sql_v3_2.py" set "RFID_DIR=%%~fD"
)
if "%RFID_DIR%"=="" (
    echo [BAD] RFID folder with UHFAPI.dll + rfid_to_sql_v3_2.py not found
) else (
    echo [OK] RFID_DIR=%RFID_DIR%
    if exist "%RFID_DIR%\venv310_32\Scripts\python.exe" (echo [OK] RFID 32-bit python) else (echo [BAD] RFID 32-bit python not found)
)

echo.
echo Done.
pause
