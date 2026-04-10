@echo off
REM ============================================================
REM  Register Local Zone edge proxy auto-start
REM  Creates a Windows Scheduled Task for the HTTPS reverse proxy.
REM
REM  Usage:
REM    scripts\local_zone\register_edge_autostart.bat <edge-domain> [caddy-exe]
REM ============================================================

setlocal EnableExtensions

if "%~1"=="" (
    echo [ERROR] Edge domain is required.
    echo Usage: scripts\local_zone\register_edge_autostart.bat ^<edge-domain^> [caddy-exe]
    exit /b 1
)

set "EDGE_DOMAIN=%~1"
set "CADDY_EXE=%~2"
if "%CADDY_EXE%"=="" set "CADDY_EXE=caddy"
set "TASK_NAME=DiagPlatform-EdgeProxy"
set "START_DELAY=0000:45"

for %%I in ("%~dp0..\\..") do set "PROJECT_DIR=%%~fI"
set "SCRIPT_PATH=%PROJECT_DIR%\scripts\local_zone\start_edge_proxy.ps1"
set "TASK_CMD=powershell -NoProfile -ExecutionPolicy Bypass -File \"%SCRIPT_PATH%\" -EdgeDomain \"%EDGE_DOMAIN%\" -CaddyExe \"%CADDY_EXE%\" -Background"

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Please run as Administrator.
    exit /b 1
)

if not exist "%SCRIPT_PATH%" (
    echo [ERROR] Edge proxy startup script not found: %SCRIPT_PATH%
    exit /b 1
)

echo Registering Local Zone edge proxy auto-start...
schtasks /create /tn "%TASK_NAME%" /tr "%TASK_CMD%" /sc onstart /delay %START_DELAY% /ru SYSTEM /rl HIGHEST /f
if errorlevel 1 (
    echo [ERROR] Failed to create scheduled task.
    exit /b 1
)

echo [OK] Registered task %TASK_NAME%
echo      Domain: %EDGE_DOMAIN%
echo      Caddy:  %CADDY_EXE%
echo      Delay:  %START_DELAY%
exit /b 0
