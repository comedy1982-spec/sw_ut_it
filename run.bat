@echo off
cd /d "%~dp0"

echo ========================================
echo   SWTS Studio
echo ========================================
echo.

python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed.
    echo         Please install Python 3.9+ from https://www.python.org
    pause
    exit /b 1
)

echo [1/4] Clearing Python cache...
for /r %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d" > nul 2>&1
)
if exist "*.pyc" del /q "*.pyc" > nul 2>&1
echo       Done.

echo [2/4] Checking dependencies...
python -c "import flask" > nul 2>&1
if errorlevel 1 (
    echo       Installing Flask...
    pip install flask > nul 2>&1
)
python -c "import clang" > nul 2>&1
if errorlevel 1 (
    echo       Installing libclang...
    pip install libclang > nul 2>&1
)
echo       Done.

echo [3/4] Checking port 5000...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5000 " ^| findstr "LISTENING"') do (
    echo       Stopping PID %%a on port 5000...
    taskkill /F /PID %%a > nul 2>&1
)
ping 127.0.0.1 -n 2 > nul
echo       Ready.

echo [4/4] Starting SWTS Studio...
echo.
echo   URL  : http://localhost:5000
echo   Stop : Press Ctrl+C
echo ========================================

set "SWTS_ROOT=%~dp0example\ecu_powertrain"
set "PORT=5000"

ping 127.0.0.1 -n 3 > nul
for /f %%i in ('powershell -command "Get-Date -UFormat %%s"') do set TS=%%i
start "" "http://localhost:5000?v=%TS%"

python app.py

echo.
echo Server stopped.
pause
