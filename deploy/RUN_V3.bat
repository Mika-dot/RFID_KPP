@echo off
setlocal
cd /d "%~dp0\.."
call "RUN_RFID_KPP_FINAL.cmd"
exit /b %errorlevel%
