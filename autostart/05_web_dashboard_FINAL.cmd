@echo off
setlocal EnableExtensions
title RFID KPP Web FINAL
call "%~dp0kpp_env_config_FINAL.cmd"
cd /d "%PROJECT_ROOT%\web" || (echo [ERROR] Cannot cd to web & pause & exit /b 1)

set "KPP_WEB_DEBUG=0"
set "KPP_WEB_REFRESH_SEC=10"
set "KPP_WEB_DEFAULT_LIMIT=200"
set "KPP_WEB_MAX_LIMIT=1000"
set "KPP_WEB_SUMMARY_HOURS=24"
set "KPP_WEB_CHART_DAYS=3"
set "KPP_WEB_DB_CONNECTION=%COMMON_DB_CONN%"
set "KPP_TASK_TABLE=dbo.RfidTags"
set "KPP_WAREHOUSE_TABLE=dbo.Warehouse"

if exist "%PROJECT_ROOT%\venv64\Scripts\activate.bat" call "%PROJECT_ROOT%\venv64\Scripts\activate.bat"
echo ============================================================
echo Web dashboard FINAL started
echo URL: http://127.0.0.1:%KPP_WEB_PORT%
echo ============================================================
python kpp_reel_dashboard_v2.7_ru.py
echo.
echo [STOP] Web dashboard stopped.
pause
