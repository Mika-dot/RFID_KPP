@echo off
setlocal EnableExtensions DisableDelayedExpansion
if "%~1"=="" goto :bad_args
if "%~2"=="" goto :bad_args
if "%~3"=="" goto :bad_args
set "PYTHON_EXE=%~1"
set "SCRIPT_FILE=%~2"
set "WORK_DIR=%~3"
if not exist "%PYTHON_EXE%" goto :bad_python
if not exist "%SCRIPT_FILE%" goto :bad_script
if not exist "%WORK_DIR%" goto :bad_workdir
cd /d "%WORK_DIR%"
if errorlevel 1 goto :bad_workdir
:restart
"%PYTHON_EXE%" -u "%SCRIPT_FILE%"
set "RC=%ERRORLEVEL%"
echo [%DATE% %TIME%] Exit code %RC%. Restarting in 5 seconds.
timeout /t 5 /nobreak >nul
goto :restart
:bad_args
echo [FATAL] RUN_SERVICE_LOOP requires Python, script and work directory arguments.
pause
exit /b 4
:bad_python
echo [FATAL] Python not found: %PYTHON_EXE%
pause
exit /b 2
:bad_script
echo [FATAL] Script not found: %SCRIPT_FILE%
pause
exit /b 3
:bad_workdir
echo [FATAL] Work directory not found: %WORK_DIR%
pause
exit /b 5
