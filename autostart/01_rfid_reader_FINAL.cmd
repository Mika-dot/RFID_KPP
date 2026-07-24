@echo off
setlocal EnableExtensions
chcp 65001 >nul
title RFID Reader FINAL FIXED

rem This file is intentionally ASCII/UTF-8-no-BOM and avoids IF (...) blocks,
rem because RFID folder name contains parentheses: Version (DB) / Версия (БД).

set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"

if exist "%SCRIPT_DIR%kpp_env_config_FINAL.cmd" call "%SCRIPT_DIR%kpp_env_config_FINAL.cmd"
if not defined COMMON_DB_CONN if exist "%SCRIPT_DIR%kpp_env_config_READY.cmd" call "%SCRIPT_DIR%kpp_env_config_READY.cmd"
if not defined COMMON_DB_CONN if exist "%SCRIPT_DIR%kpp_env_config.bat" call "%SCRIPT_DIR%kpp_env_config.bat"
if not defined COMMON_DB_CONN goto ERR_CONFIG

set "LOG_DIR=%PROJECT_ROOT%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "RFID_LAST_LOG=%LOG_DIR%\rfid_reader_final_fixed_last_run.txt"

echo ============================================================
echo RFID Reader FINAL FIXED
echo Root: %PROJECT_ROOT%
echo ============================================================
echo.

if not exist "%PROJECT_ROOT%" goto ERR_ROOT

set "RFID_DIR="
for /d %%D in ("%PROJECT_ROOT%\RFID_readers\*") do call :TRY_RFID "%%~fD"
if not defined RFID_DIR goto ERR_NO_RFID

echo [OK] RFID_DIR=%RFID_DIR%

set "PY_EXE=%RFID_DIR%\venv310_32\Scripts\python.exe"
if not exist "%PY_EXE%" goto ERR_PY

echo [OK] PY_EXE=%PY_EXE%

pushd "%RFID_DIR%"
if errorlevel 1 goto ERR_CD

set "RFID_DLL_PATH=%RFID_DIR%\UHFAPI.dll"
set "RFID_DB_CONNECTION=%COMMON_DB_CONN%"
set "RFID_CONSOLE_OUTPUT=1"
set "RFID_BATCH_SIZE=1"
set "RFID_BATCH_FLUSH_SEC=0.5"
set "RFID_DEDUP_ENABLED=0"
set "RFID_RECONNECT_DELAY_SEC=5"
set "RFID_ENABLE_EPC_TID_MODE=1"
set "RFID_EPC_TID_SAVE=1"
set "RFID_REQUIRE_TID=1"
set "RFID_EMPTY_TID_WARN_EVERY=10"
set "RFID_READ_SLEEP_SEC=0.01"
set "RFID_IDLE_SLEEP_SEC=0.05"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo [INFO] Reader: %RFID_READER_IP%:%RFID_READER_PORT%
echo [INFO] DB: %SQL_SERVER% / %SQL_DATABASE%
echo [INFO] DLL: %RFID_DLL_PATH%
echo [INFO] Starting RFID Python now...
echo.

echo START %DATE% %TIME% > "%RFID_LAST_LOG%"
echo RFID_DIR=%RFID_DIR% >> "%RFID_LAST_LOG%"
echo PY_EXE=%PY_EXE% >> "%RFID_LAST_LOG%"
echo READER=%RFID_READER_IP%:%RFID_READER_PORT% >> "%RFID_LAST_LOG%"

"%PY_EXE%" -u rfid_to_sql_v3_2.py
set "RC=%ERRORLEVEL%"

echo.
echo [STOP] RFID Reader exited with code %RC%.
echo STOP %DATE% %TIME% code=%RC% >> "%RFID_LAST_LOG%"
popd
pause
exit /b %RC%

:TRY_RFID
if defined RFID_DIR exit /b 0
if not exist "%~1\UHFAPI.dll" exit /b 0
if not exist "%~1\rfid_to_sql_v3_2.py" exit /b 0
if not exist "%~1\venv310_32\Scripts\python.exe" exit /b 0
set "RFID_DIR=%~1"
exit /b 0

:ERR_CONFIG
echo [ERROR] Cannot load DB config.
echo Tried:
echo   %SCRIPT_DIR%kpp_env_config_FINAL.cmd
echo   %SCRIPT_DIR%kpp_env_config_READY.cmd
echo   %SCRIPT_DIR%kpp_env_config.bat
pause
exit /b 1

:ERR_ROOT
echo [ERROR] Project root not found: %PROJECT_ROOT%
pause
exit /b 1

:ERR_NO_RFID
echo [ERROR] Cannot find RFID folder.
echo Need folder under: %PROJECT_ROOT%\RFID_readers
echo Required files: UHFAPI.dll, rfid_to_sql_v3_2.py, venv310_32\Scripts\python.exe
pause
exit /b 1

:ERR_PY
echo [ERROR] 32-bit Python venv not found:
echo %PY_EXE%
echo Fix/copy venv310_32 first.
pause
exit /b 1

:ERR_CD
echo [ERROR] Cannot cd to RFID_DIR:
echo %RFID_DIR%
pause
exit /b 1
