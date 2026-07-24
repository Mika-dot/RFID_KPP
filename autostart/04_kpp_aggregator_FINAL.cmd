@echo off
setlocal EnableExtensions
title KPP Aggregator FINAL
call "%~dp0kpp_env_config_FINAL.cmd"
cd /d "%PROJECT_ROOT%\KPP" || (echo [ERROR] Cannot cd to KPP & pause & exit /b 1)

set "KPP_CONN_STR=DRIVER={%SQL_DRIVER%};SERVER=%SQL_SERVER%;DATABASE=%SQL_DATABASE%;UID=%SQL_USER%;PWD=%SQL_PASSWORD%;Encrypt=yes;TrustServerCertificate=yes;"
set "KPP_TASK_CONN_STR=%KPP_CONN_STR%"
set "KPP_RFID_TABLE=dbo.RFID_Tags"
set "KPP_VIDEO_TABLE=dbo.ReelTransitions"
set "KPP_SKUD_TABLE=dbo.RusGuardLogs"
set "KPP_TASK_TABLE=dbo.RfidTags"
set "KPP_EVENT_TABLE=dbo.KPP_ReelEvents"
set "KPP_STATE_TABLE=dbo.KPP_RuntimeState"
set "KPP_TASK_ID_COL=Id"
set "KPP_TASK_DT_COL=Dt"
set "KPP_TASK_TAG_COL=Tag"
set "KPP_TASK_DOCIDS_COL=Ids"
set "KPP_FULL_REBUILD_ON_START=0"
set "KPP_CONTINUE_LIVE_AFTER_REBUILD=1"
set "KPP_TASK_LOAD_MODE=LOOKBACK"
set "KPP_TASK_LOOKBACK_HOURS=24"
set "KPP_TASK_MATCH_WARN_DELTA_HOURS=24"
set "KPP_PENDING_RECHECK_HOURS=24"
set "KPP_RECHECK_DELAY_SEC=300"
set "KPP_MAX_RECHECK_COUNT=288"
set "KPP_WAREHOUSE_ENABLED=1"
set "KPP_WAREHOUSE_TABLE=dbo.Warehouse"
set "KPP_WAREHOUSE_LOAD_MODE=LOOKBACK"
set "KPP_WAREHOUSE_LOOKBACK_HOURS=24"
set "KPP_WAREHOUSE_MATCH_WINDOW_HOURS=24"
set "KPP_WAREHOUSE_ONLY_GRACE_MINUTES=10"
set "KPP_POLL_INTERVAL_SEC=3"
set "KPP_TASK_RELOAD_INTERVAL_SEC=30"
set "KPP_WAREHOUSE_RELOAD_INTERVAL_SEC=30"
set "KPP_WAREHOUSE_EMIT_INTERVAL_SEC=60"
set "KPP_ENRICH_INTERVAL_SEC=60"
set "KPP_STATUS_INTERVAL_SEC=30"
set "KPP_RFID_DISAPPEAR_TIMEOUT_SEC=35"
set "KPP_SESSION_MAX_DURATION_SEC=900"
set "KPP_RECOVERY_LOOKBACK_MINUTES=20"
set "KPP_START_FROM_LATEST_IF_NO_STATE=1"
set "KPP_PRINT_REPORTS=1"

if exist "%PROJECT_ROOT%\venv64\Scripts\activate.bat" call "%PROJECT_ROOT%\venv64\Scripts\activate.bat"
echo ============================================================
echo KPP Aggregator FINAL started
echo DB: %SQL_SERVER% / %SQL_DATABASE%
echo Full rebuild on start: %KPP_FULL_REBUILD_ON_START%
echo ============================================================
python kpp_1_reliable_v2.4_full_rebuild.py
echo.
echo [STOP] KPP Aggregator stopped.
pause
