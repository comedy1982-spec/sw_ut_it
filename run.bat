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
if exist __pycache__ rd /s /q __pycache__
echo       Done.

echo [2/4] Checking dependencies...
python -c "import flask" > nul 2>&1
if errorlevel 1 (
    echo       Installing Flask...
    pip install flask > nul 2>&1
)
echo       Done.

echo [3/4] Checking port 5000...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5000 " ^| findstr "LISTENING"') do (
    echo       Port 5000 in use by PID %%a - terminating...
    taskkill /F /PID %%a > nul 2>&1
)
ping 127.0.0.1 -n 2 > nul
echo       Ready.

echo [4/4] Starting server...
echo.
echo   URL : http://localhost:5000
echo   Press Ctrl+C to stop.
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
