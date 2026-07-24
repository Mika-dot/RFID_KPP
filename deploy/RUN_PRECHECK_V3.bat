@echo off
setlocal
cd /d "%~dp0"
if not exist "config_v3.cmd" (
  echo [ERROR] Нет deploy\config_v3.cmd
  if /I not "%~1"=="/nopause" pause
  exit /b 1
)
call "config_v3.cmd"
if not exist "%ROOT%\venv64\Scripts\python.exe" (
  echo [ERROR] Не найден %ROOT%\venv64\Scripts\python.exe
  if /I not "%~1"=="/nopause" pause
  exit /b 1
)
if not exist "%ROOT%\venv310_32\Scripts\python.exe" (
  echo [ERROR] Не найден %ROOT%\venv310_32\Scripts\python.exe
  if /I not "%~1"=="/nopause" pause
  exit /b 1
)
"%ROOT%\venv310_32\Scripts\python.exe" -c "import pyodbc; print('[OK] 32-bit pyodbc')"
if errorlevel 1 goto :fail
"%ROOT%\venv64\Scripts\python.exe" "%ROOT%\deploy\precheck_v3.py"
if errorlevel 1 goto :fail
if /I not "%~1"=="/nopause" pause
exit /b 0
:fail
echo [ERROR] PRECHECK не пройден. Сервисы не запускаются.
if /I not "%~1"=="/nopause" pause
exit /b 1
