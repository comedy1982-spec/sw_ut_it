@echo off
cd /d "%~dp0"

echo ========================================
echo   SWTS Studio
echo ========================================
echo.

:: ── Python 확인 ──
python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.9+ from https://www.python.org
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo [Python] %%v
for /f "tokens=*" %%p in ('python -c "import sys; print(sys.executable)"') do echo [Path]   %%p
echo.

echo [1/4] Clearing Python cache...
for /r %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d" > nul 2>&1
)
echo       Done.

echo [2/4] Checking dependencies...
python -c "import flask" > nul 2>&1
if errorlevel 1 (
    echo       Installing Flask...
    python -m pip install flask -q
)

python -c "import clang.cindex; clang.cindex.Index.create()" > nul 2>&1
if errorlevel 1 (
    echo       Installing libclang...
    python -m pip install libclang -q
    python -c "import clang.cindex; clang.cindex.Index.create()" > nul 2>&1
    if errorlevel 1 (
        echo [WARN] libclang 로드 실패 -- 정적 분석 모드로 실행됩니다.
    ) else (
        echo       libclang: OK
    )
) else (
    echo       libclang: OK
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
echo.

set "SWTS_ROOT=%~dp0example\ecu_powertrain"
set "PORT=5000"

ping 127.0.0.1 -n 3 > nul
for /f %%i in ('powershell -command "Get-Date -UFormat %%s"') do set TS=%%i
start "" "http://localhost:5000?v=%TS%"

python app.py

echo.
echo Server stopped.
pause
