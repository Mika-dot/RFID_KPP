@echo off
setlocal
set "PYTHON_EXE=%~1"
set "SCRIPT_FILE=%~2"
set "WORK_DIR=%~3"
if not exist "%PYTHON_EXE%" (
  echo [FATAL] Python не найден: %PYTHON_EXE%
  pause
  exit /b 2
)
if not exist "%SCRIPT_FILE%" (
  echo [FATAL] Скрипт не найден: %SCRIPT_FILE%
  pause
  exit /b 3
)
pushd "%WORK_DIR%"
:restart
"%PYTHON_EXE%" "%SCRIPT_FILE%"
set "RC=%ERRORLEVEL%"
echo [%DATE% %TIME%] Процесс завершён с кодом %RC%. Повтор через 5 секунд. Закройте это окно для остановки.
timeout /t 5 /nobreak >nul
goto restart
