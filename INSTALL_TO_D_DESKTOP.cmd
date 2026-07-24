@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo This will copy this FINAL project to D:\Desktop\RFID_KPP-main
echo Existing files with the same names will be overwritten.
echo.
set /p CONFIRM=Type INSTALL to continue: 
if /I not "%CONFIRM%"=="INSTALL" exit /b 0
if not exist "D:\Desktop" mkdir "D:\Desktop"
robocopy "%CD%" "D:\Desktop\RFID_KPP-main" /E /XO /R:2 /W:1
if %ERRORLEVEL% LEQ 7 echo Install finished.
if %ERRORLEVEL% GTR 7 echo Install finished with robocopy errors: %ERRORLEVEL%
pause
