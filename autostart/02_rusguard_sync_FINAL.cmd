@echo off
setlocal EnableExtensions
title RusGuard Sync FINAL
call "%~dp0kpp_env_config_FINAL.cmd"
cd /d "%PROJECT_ROOT%\DB_RusGard" || (echo [ERROR] Cannot cd to DB_RusGard & pause & exit /b 1)

if exist "%PROJECT_ROOT%\venv64\Scripts\activate.bat" call "%PROJECT_ROOT%\venv64\Scripts\activate.bat"
echo ============================================================
echo RusGuard Sync FINAL started
echo ============================================================
python db_sync.py
echo.
echo [STOP] RusGuard Sync stopped.
pause
