@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo This script deletes obsolete files from the CURRENT folder.
echo Make a backup first.
echo.
set /p CONFIRM=Type DELETE_OLD to continue: 
if /I not "%CONFIRM%"=="DELETE_OLD" exit /b 0

for %%F in (
"RUN_KPP_ALL.bat"
"RUN_RFID_KPP_READY.cmd"
"RUN_RFID_ONLY_READY_V2.cmd"
"CHECK_KPP_AUTOSTART_ENV.bat"
"INSTALL_AUTOSTART_USER.bat"
"UNINSTALL_AUTOSTART_USER.bat"
"INSTALL_READY_FILES.cmd"
"INSTALL_RFID_READER_FIX_V2.cmd"
"INSTALL_FINAL_FILES.cmd"
"INSTALL_RFID_FINAL_FIXED.cmd"
"PATCH_README.md"
"README_RFID_FIX_V2.txt"
"README_RUN_NOW.txt"
"README_AUTOSTART.md"
"ReelTransitions"
"SQL"
"autostart\01_rfid_reader.bat"
"autostart\02_rusguard_sync.bat"
"autostart\03_rtsp_yolo.bat"
"autostart\04_kpp_aggregator.bat"
"autostart\05_web_dashboard.bat"
"autostart\kpp_env_config.bat"
"autostart\01_rfid_reader_READY.cmd"
"autostart\01_rfid_reader_READY_V2.cmd"
"autostart\02_rusguard_sync_READY.cmd"
"autostart\03_rtsp_yolo_READY.cmd"
"autostart\04_kpp_aggregator_READY.cmd"
"autostart\05_web_dashboard_READY.cmd"
"autostart\kpp_env_config_READY.cmd"
"KPP\kpp_1_reliable_v1.py"
"KPP\kpp_1_reliable_v2.py"
"KPP\kpp_1_reliable_v2.1.py"
"KPP\kpp_1_reliable_v2.2.py"
"KPP\kpp_1_reliable_v2.3_full_rebuild.py"
"KPP\kpp_v1.py"
"KPP\kpp_v1.2.py"
"web\kpp_reel_dashboard.py"
"web\kpp_reel_dashboard_v2.py"
"web\kpp_reel_dashboard_v2_ru.py"
"web\kpp_reel_dashboard_v2.2_ru.py"
"web\kpp_reel_dashboard_v2.3_ru.py"
"web\kpp_reel_dashboard_v2.4_ru.py"
"web\kpp_reel_dashboard_v2.5_ru.py"
"web\kpp_reel_dashboard_v2.6_ru.py"
"RTSP\RTSP.py"
"RTSP\RTSP_yolo.py"
"RTSP\RTSP_yolo_DB.py"
"RTSP\train_yolo.py"
"RTSP\split_dataset.py"
"RTSP\Запуск.txt"
"RTSP\Сздание таблицы.sql"
"RFID_readers\Версия (БД)\rfid_to_sql.py"
"RFID_readers\Версия (БД)\Запуск.txt"
"RFID_readers\Версия (БД)\Сздание таблицы.sql"
"tools\check_recent_rfid_tid.sql"
) do if exist %%F del /f /q %%F

for %%F in ("RTSP\recordings.zip.*") do if exist %%F del /f /q %%F
if exist "RFID_readers\Версия (консоль)" rmdir /s /q "RFID_readers\Версия (консоль)"

echo Done.
pause
