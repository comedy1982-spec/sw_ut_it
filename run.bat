@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ========================================
echo   SWTS Studio
echo ========================================
echo.

python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo         Install Python 3.9+ from https://www.python.org
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo [Python] %%v
for /f "tokens=*" %%p in ('python -c "import sys; print(sys.executable)"') do echo [Path]   %%p
echo.

echo [1/4] Clearing cache...
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
)
echo       Done.

echo [2.5/4] Detecting Clang MC/DC toolchain...
set "MINGW_BIN="
for /d %%d in ("%USERPROFILE%\llvm-mingw-*") do set "MINGW_BIN=%%d\bin"
if defined MINGW_BIN if exist "!MINGW_BIN!\clang.exe" (
    set "PATH=!MINGW_BIN!;%PATH%"
    echo       Clang MC/DC engine: !MINGW_BIN! ^(llvm-mingw^)
    goto :clang_done
)
where clang >nul 2>&1 && (
    for /f "delims=" %%c in ('where clang 2^>nul') do (
        echo       Clang MC/DC engine: %%c ^(system PATH^)
        goto :clang_done
    )
)
echo       Clang not found - static analysis mode.
echo       ^(install LLVM + VS Build Tools, or unzip llvm-mingw to %%USERPROFILE%%^)
:clang_done

echo [3/4] Force-stopping anything on port 5000...
set "_cleared=0"
for /L %%i in (1,1,6) do (
    set "_found=0"
    for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5000 " ^| findstr "LISTENING"') do (
        set "_found=1"
        set "_cleared=1"
        echo       Killing PID %%a ...
        taskkill /F /T /PID %%a > nul 2>&1
    )
    if "!_found!"=="0" goto :port_free
    ping 127.0.0.1 -n 2 > nul
)
:port_free
if "!_cleared!"=="1" (
    echo       Port 5000 cleared.
) else (
    echo       Port 5000 was already free.
)

echo [4/4] Starting server...
echo.
echo   URL  : http://localhost:5000
echo   Stop : Ctrl+C
echo ========================================

set "SWTS_ROOT=%~dp0example\ecu_powertrain"
set "PORT=5000"

ping 127.0.0.1 -n 3 > nul
for /f %%i in ('powershell -command "Get-Date -UFormat %%s"') do set "TS=%%i"
start "" "http://localhost:5000?v=!TS!"

python app.py

echo.
echo Server stopped.
pause
endlocal
