@echo off
REM ============================================================
REM  Cloud Services Startup Script
REM  Starts all required services for the diagnostics platform:
REM    1. VCI Proxy reverse server (port 9000/9001)
REM    2. Flask API server (port 8080)
REM    3. GDS2 with Java Agent
REM
REM  Usage:
REM    scripts\cloud_start_services.bat          - Start all services
REM    scripts\cloud_start_services.bat --no-gds2 - Skip GDS2 launch
REM ============================================================

setlocal enabledelayedexpansion

for %%I in ("%~dp0..") do set "DEFAULT_PROJECT_DIR=%%~fI"
if not defined PROJECT_DIR set "PROJECT_DIR=%DEFAULT_PROJECT_DIR%"
if not defined VENV_DIR set "VENV_DIR=%PROJECT_DIR%\venv32"
if not defined GDS2_AGENT_DIR set "GDS2_AGENT_DIR=C:\tools\gds2-agent"
if not defined LOG_DIR set "LOG_DIR=%PROJECT_DIR%\logs"
set "SKIP_GDS2=0"
set "PYTHON_EXE="

if defined PYTHON if exist "%PYTHON%" set "PYTHON_EXE=%PYTHON%"
if not defined PYTHON_EXE if exist "%PROJECT_DIR%\venv32\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%\venv32\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%PROJECT_DIR%\venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%\venv\Scripts\python.exe"
if not defined PYTHON_EXE (
    for /f "usebackq delims=" %%I in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do (
        if not defined PYTHON_EXE set "PYTHON_EXE=%%I"
    )
)
if not defined PYTHON_EXE (
    for /f "usebackq delims=" %%I in (`python -c "import sys; print(sys.executable)" 2^>nul`) do (
        if not defined PYTHON_EXE set "PYTHON_EXE=%%I"
    )
)

REM Parse arguments
if "%1"=="--no-gds2" set "SKIP_GDS2=1"

REM Session 0 startup tasks should only launch headless services.
if "%SKIP_GDS2%"=="0" (
    if /I "%SESSIONNAME%"=="Services" (
        echo [INFO] Session 0 detected, skipping GDS2 auto-launch.
        set "SKIP_GDS2=1"
    )
)

echo ============================================================
echo  Diagnostic Platform - Cloud Services
echo ============================================================
echo.
echo  Project dir: %PROJECT_DIR%
if defined PYTHON_EXE (
    echo  Python:      %PYTHON_EXE%
) else (
    echo  Python:      [not found]
)
echo.

REM Check prerequisites
if not defined PYTHON_EXE (
    echo [ERROR] Python not found.
    echo Checked:
    echo   - %%PYTHON%% env var
    echo   - %PROJECT_DIR%\venv32\Scripts\python.exe
    echo   - %PROJECT_DIR%\venv\Scripts\python.exe
    echo   - py -3 ^(resolved to sys.executable^)
    echo   - python on PATH ^(resolved to sys.executable^)
    exit /b 1
)

REM Create log directory
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ---- 1. Start VCI Proxy Reverse Server ----
call :is_port_listening 127.0.0.1 9000
if errorlevel 1 (
    echo [1/3] Starting VCI Proxy reverse server...
    start "VCI-Proxy-Server" /D "%PROJECT_DIR%" /MIN "%ComSpec%" /c """%PYTHON_EXE%"" -m vci_proxy.reverse_server > ""%LOG_DIR%\vci_proxy.log"" 2>&1"
    call :wait_for_port 127.0.0.1 9000 15
    if errorlevel 1 (
        echo       [WARN] Port 9000 did not become ready in time. Check %LOG_DIR%\vci_proxy.log
    ) else (
        echo       Listening on ports 9000 (VCI) and 9001 (proxy)
    )
) else (
    echo [1/3] VCI Proxy reverse server already listening on port 9000, skipping start.
)

REM ---- 2. Start Flask API Server ----
call :is_port_listening 127.0.0.1 8080
if errorlevel 1 (
    echo [2/3] Starting Flask API server...
    start "Flask-API" /D "%PROJECT_DIR%" /MIN "%ComSpec%" /c """%PYTHON_EXE%"" app.py --port 8080 > ""%LOG_DIR%\flask_api.log"" 2>&1"
    call :wait_for_port 127.0.0.1 8080 15
    if errorlevel 1 (
        echo       [WARN] Port 8080 did not become ready in time. Check %LOG_DIR%\flask_api.log
    ) else (
        echo       Listening on port 8080
    )
) else (
    echo [2/3] Flask API server already listening on port 8080, skipping start.
)

REM ---- 3. Start GDS2 with Java Agent ----
if "%SKIP_GDS2%"=="1" (
    echo [3/3] Skipping GDS2 launch
) else (
    if exist "%GDS2_AGENT_DIR%\launch-gds2-with-agent.bat" (
        echo [3/3] Starting GDS2 with Java Agent...
        start "GDS2" cmd /c """%GDS2_AGENT_DIR%\launch-gds2-with-agent.bat"""
        timeout /t 3 /nobreak >nul
        echo       GDS2 launched with agent (interval=100ms)
    ) else (
        echo [3/3] GDS2 agent launcher not found, skipping
    )
)

echo.
echo ============================================================
echo  All services started!
echo ============================================================
echo.
echo  VCI Proxy:  ports 9000/9001  (log: %LOG_DIR%\vci_proxy.log)
echo  Flask API:  port 8080        (log: %LOG_DIR%\flask_api.log)
if "%SKIP_GDS2%"=="0" echo  GDS2:       running with agent
echo.
echo  To stop all services:
echo    taskkill /FI "WINDOWTITLE eq VCI-Proxy-Server" /F
echo    taskkill /FI "WINDOWTITLE eq Flask-API" /F
echo ============================================================
exit /b 0

:is_port_listening
powershell -NoProfile -Command "$ready = Test-NetConnection -ComputerName '%~1' -Port %~2 -InformationLevel Quiet -WarningAction SilentlyContinue; if ($ready) { exit 0 } else { exit 1 }" >nul 2>&1
exit /b %errorlevel%

:wait_for_port
for /l %%I in (1,1,%~3) do (
    call :is_port_listening %~1 %~2
    if not errorlevel 1 exit /b 0
    timeout /t 2 /nobreak >nul
)
exit /b 1
