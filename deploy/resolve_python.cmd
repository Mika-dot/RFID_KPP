@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "PY64="
set "PY32="

for %%P in (
  "%ROOT%\venv64\Scripts\python.exe"
  "%ROOT%\.venv\Scripts\python.exe"
  "%ROOT%\venv\Scripts\python.exe"
  "%ROOT%\venv310\Scripts\python.exe"
  "%ROOT%\venv310_32\Scripts\python.exe"
  "C:\Users\perestoroninAM\AppData\Local\Programs\Python\Python310-32\python.exe"
) do (
  if exist "%%~fP" (
    "%%~fP" -c "import struct,sys;sys.exit(0 if struct.calcsize('P')*8==64 else 1)" >nul 2>&1
    if not errorlevel 1 if not defined PY64 set "PY64=%%~fP"
    "%%~fP" -c "import struct,sys;sys.exit(0 if struct.calcsize('P')*8==32 else 1)" >nul 2>&1
    if not errorlevel 1 if not defined PY32 set "PY32=%%~fP"
  )
)

for /r "%ROOT%" %%P in (python.exe) do (
  if exist "%%~fP" (
    "%%~fP" -c "import struct,sys;sys.exit(0 if struct.calcsize('P')*8==64 else 1)" >nul 2>&1
    if not errorlevel 1 if not defined PY64 set "PY64=%%~fP"
    "%%~fP" -c "import struct,sys;sys.exit(0 if struct.calcsize('P')*8==32 else 1)" >nul 2>&1
    if not errorlevel 1 if not defined PY32 set "PY32=%%~fP"
  )
)

if not defined PY64 (
  for /f "usebackq delims=" %%P in (`py -3.10-64 -c "import sys;print(sys.executable)" 2^>nul`) do if not defined PY64 set "PY64=%%P"
)
if not defined PY32 (
  for /f "usebackq delims=" %%P in (`py -3.10-32 -c "import sys;print(sys.executable)" 2^>nul`) do if not defined PY32 set "PY32=%%P"
)
if not defined PY64 (
  for /f "usebackq delims=" %%P in (`python -c "import struct,sys;print(sys.executable if struct.calcsize('P')*8==64 else '')" 2^>nul`) do if not "%%P"=="" set "PY64=%%P"
)

if not defined PY64 (
  echo [FATAL] 64-bit Python was not found.
  endlocal & exit /b 1
)
if not defined PY32 (
  echo [FATAL] 32-bit Python 3.10 was not found. The RFID DLL requires 32-bit Python.
  endlocal & exit /b 1
)

endlocal & set "PY64=%PY64%" & set "PY32=%PY32%" & exit /b 0
