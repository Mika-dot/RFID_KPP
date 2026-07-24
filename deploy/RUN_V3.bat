@echo off
setlocal
cd /d "%~dp0"
if not exist "config_v3.cmd" (
  echo [ERROR] Скопируйте config_v3.example.cmd в config_v3.cmd и заполните параметры.
  pause
  exit /b 1
)
call "config_v3.cmd"
if not exist "%ROOT%\runtime" mkdir "%ROOT%\runtime"

if not exist "%ROOT%\venv64\Scripts\python.exe" (
  echo [ERROR] Не найдено 64-bit окружение: %ROOT%\venv64
  pause
  exit /b 1
)
if not exist "%ROOT%\venv310_32\Scripts\python.exe" (
  echo [ERROR] Не найдено 32-bit окружение: %ROOT%\venv310_32
  pause
  exit /b 1
)

call "%ROOT%\deploy\RUN_PRECHECK_V3.bat" /nopause
if errorlevel 1 (
  pause
  exit /b 1
)

start "1 RFID Reader v4" "%ROOT%\deploy\RUN_SERVICE_LOOP.bat" "%ROOT%\venv310_32\Scripts\python.exe" "%ROOT%\RFID_reader_v4\rfid_to_sql_v4.py" "%ROOT%\RFID_reader_v4"
start "2 RusGuard Sync v2" "%ROOT%\deploy\RUN_SERVICE_LOOP.bat" "%ROOT%\venv64\Scripts\python.exe" "%ROOT%\DB_RusGard\db_sync_v2.py" "%ROOT%\DB_RusGard"
start "3 RTSP YOLO v3" "%ROOT%\deploy\RUN_SERVICE_LOOP.bat" "%ROOT%\venv64\Scripts\python.exe" "%ROOT%\RTSP\RTSP_yolo_DB_v3.py" "%ROOT%\RTSP"
start "4 KPP Aggregator v3" "%ROOT%\deploy\RUN_SERVICE_LOOP.bat" "%ROOT%\venv64\Scripts\python.exe" "%ROOT%\KPP\kpp_aggregator_v3.py" "%ROOT%\KPP"
start "5 WEB v3" "%ROOT%\deploy\RUN_SERVICE_LOOP.bat" "%ROOT%\venv64\Scripts\python.exe" "%ROOT%\web\kpp_reel_dashboard_v3_ru.py" "%ROOT%\web"
endlocal
