@echo off
setlocal
"%PY64%" -c "import pyodbc,flask,waitress,numpy,cv2,ultralytics" >nul 2>&1
if errorlevel 1 (
  echo [INFO] Installing missing 64-bit packages...
  "%PY64%" -m pip install -r "%ROOT%\requirements_v3.txt"
  if errorlevel 1 exit /b 1
)
"%PY32%" -c "import pyodbc" >nul 2>&1
if errorlevel 1 (
  echo [INFO] Installing missing 32-bit packages...
  "%PY32%" -m pip install -r "%ROOT%\requirements_reader_v4.txt"
  if errorlevel 1 exit /b 1
)
echo [OK] Python dependencies are ready.
exit /b 0
