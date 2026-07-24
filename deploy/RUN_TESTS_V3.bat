@echo off
setlocal
cd /d "%~dp0\.."
if not exist "venv64\Scripts\python.exe" (
  echo [ERROR] Не найден venv64\Scripts\python.exe
  echo Сначала создайте 64-битное окружение и установите requirements_v3.txt
  pause
  exit /b 1
)
"venv64\Scripts\python.exe" tests\run_tests.py
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
