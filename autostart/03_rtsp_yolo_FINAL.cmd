@echo off
setlocal EnableExtensions
title RTSP YOLO FINAL
call "%~dp0kpp_env_config_FINAL.cmd"
cd /d "%PROJECT_ROOT%\RTSP" || (echo [ERROR] Cannot cd to RTSP & pause & exit /b 1)
if not exist recordings mkdir recordings

set "RFID_DB_CONNECTION=%COMMON_DB_CONN%"
set "RFID_DB_LOG_TABLE=ReelTransitions"
set "RFID_MODEL_PATH=runs\detect\rfid_forklift_reel2\weights\best.pt"
set "RFID_CONFIDENCE_THRESHOLD=0.25"
set "RFID_IOU_THRESHOLD=0.45"
set "RFID_ENABLE_DETECTION=True"
set "RFID_RTSP_0=%RFID_RTSP_0%"
set "RFID_RTSP_1=%RFID_RTSP_1%"
set "RFID_MASK_ENABLED=True"
set "RFID_MASK_0=%PROJECT_ROOT%\RTSP\Mask_0.jpg"
set "RFID_MASK_1=%PROJECT_ROOT%\RTSP\Mask_1.jpg"
set "RFID_SAVE_IMAGE_ON_TRANSITION=True"
set "RFID_IMAGE_QUALITY=70"
set "RFID_IMAGE_MAX_WIDTH=320"
set "RFID_IMAGE_MAX_HEIGHT=240"
set "RFID_LOG_DETECTIONS=True"
set "RFID_LOG_THROTTLE_MS=1500"
set "RFID_CSV_LOG_PATH=recordings\detections_log.csv"
set "RFID_REEL_TRACKING_ENABLED=True"
set "RFID_REEL_CLASS_NAME=cable_reel"
set "RFID_TRANSITION_WINDOW_SEC=30.0"
set "RFID_REEL_DISAPPEAR_SEC=5.0"
set "RFID_REEL_NEARBY_THRESHOLD_PX=300"
set "RFID_REEL_TRANSITION_LOG_PATH=recordings\reel_transitions.csv"
set "RFID_TRACK_MERGE_DISTANCE_PX=100"
set "RFID_EVENT_COOLDOWN_SEC=2.0"
set "RFID_WINDOW_WIDTH=320"
set "RFID_WINDOW_HEIGHT=240"

if exist "%PROJECT_ROOT%\venv64\Scripts\activate.bat" call "%PROJECT_ROOT%\venv64\Scripts\activate.bat"
echo ============================================================
echo RTSP YOLO FINAL started
echo ============================================================
python RTSP_yolo_DB_v2.py
echo.
echo [STOP] RTSP YOLO stopped.
pause
