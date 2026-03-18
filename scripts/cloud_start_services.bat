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

set "PROJECT_DIR=C:\gds2-automation"
set "VENV_DIR=%PROJECT_DIR%\venv32"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "GDS2_AGENT_DIR=C:\tools\gds2-agent"
set "LOG_DIR=%PROJECT_DIR%\logs"
set "SKIP_GDS2=0"

REM Parse arguments
if "%1"=="--no-gds2" set "SKIP_GDS2=1"

echo ============================================================
echo  Diagnostic Platform - Cloud Services
echo ============================================================
echo.

REM Check prerequisites
if not exist "%PYTHON%" (
    echo [ERROR] Python not found at: %PYTHON%
    echo Please set up venv32 first.
    exit /b 1
)

REM Create log directory
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ---- 1. Start VCI Proxy Reverse Server ----
echo [1/3] Starting VCI Proxy reverse server...
start "VCI-Proxy-Server" /MIN cmd /c "cd /d %PROJECT_DIR% && %PYTHON% -m vci_proxy.reverse_server > %LOG_DIR%\vci_proxy.log 2>&1"
timeout /t 2 /nobreak >nul
echo       Listening on ports 9000 (VCI) and 9001 (proxy)

REM ---- 2. Start Flask API Server ----
echo [2/3] Starting Flask API server...
start "Flask-API" /MIN cmd /c "cd /d %PROJECT_DIR% && %PYTHON% app.py --port 8080 > %LOG_DIR%\flask_api.log 2>&1"
timeout /t 2 /nobreak >nul
echo       Listening on port 8080

REM ---- 3. Start GDS2 with Java Agent ----
if "%SKIP_GDS2%"=="1" (
    echo [3/3] Skipping GDS2 launch (--no-gds2 flag)
) else (
    if exist "%GDS2_AGENT_DIR%\launch-gds2-with-agent.bat" (
        echo [3/3] Starting GDS2 with Java Agent...
        start "GDS2" cmd /c "%GDS2_AGENT_DIR%\launch-gds2-with-agent.bat"
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
