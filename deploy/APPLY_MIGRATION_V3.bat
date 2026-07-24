@echo off
setlocal
cd /d "%~dp0"
if not exist config_v3.cmd (
  echo [ERROR] Нет deploy\config_v3.cmd
  pause
  exit /b 1
)
call config_v3.cmd
where sqlcmd >nul 2>nul || (echo [ERROR] Не найден sqlcmd. Выполните migrations\001_kpp_v3_reliability.sql через SSMS.& pause & exit /b 1)

set /p "SQL_SERVER=SQL Server: "
set /p "SQL_DATABASE=Database: "
set /p "SQL_AUTH=Windows authentication? [Y/n]: "
if /I "%SQL_AUTH%"=="n" goto sql_auth

sqlcmd -S "%SQL_SERVER%" -d "%SQL_DATABASE%" -E -C -b -i "%ROOT%\migrations\001_kpp_v3_reliability.sql"
goto check_result

:sql_auth
set /p "SQL_USER=SQL user: "
if not defined SQLCMDPASSWORD (
  echo [ERROR] Для SQL authentication заранее задайте пароль только в текущей консоли:
  echo         set SQLCMDPASSWORD=ваш_пароль
  echo Пароль не запрашивается и не передаётся через аргумент -P.
  pause
  exit /b 1
)
sqlcmd -S "%SQL_SERVER%" -d "%SQL_DATABASE%" -U "%SQL_USER%" -C -b -i "%ROOT%\migrations\001_kpp_v3_reliability.sql"

:check_result
if errorlevel 1 (echo [ERROR] Миграция не применена.& pause & exit /b 1)
echo [OK] Миграция v3.1 применена.
pause
endlocal
