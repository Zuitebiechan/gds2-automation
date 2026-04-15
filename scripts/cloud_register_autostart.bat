@echo off
REM ============================================================
REM  Register Cloud Services for Auto-Start on Boot
REM  Creates Windows Scheduled Tasks that run at system startup.
REM
REM  Run this ONCE on the cloud server (as Administrator).
REM  After this, services will auto-start on every boot/reboot.
REM
REM  To unregister: scripts\cloud_unregister_autostart.bat
REM ============================================================

setlocal enabledelayedexpansion

for %%I in ("%~dp0..") do set "DEFAULT_PROJECT_DIR=%%~fI"
if not defined PROJECT_DIR set "PROJECT_DIR=%DEFAULT_PROJECT_DIR%"
if not defined START_DELAY set "START_DELAY=0000:30"
set "TASK_NAME=DiagPlatform-AutoStart"

echo ============================================================
echo  Register Auto-Start Services
echo ============================================================
echo.

set "SCRIPT_PATH=%PROJECT_DIR%\scripts\cloud_start_services.bat"
set "CONFIG_PATH=%PROJECT_DIR%\scripts\cloud_service_config.cmd"
set "CONFIG_EXAMPLE=%PROJECT_DIR%\scripts\cloud_service_config.example.cmd"
set "TASK_CMD=%ComSpec% /c \"\"%SCRIPT_PATH%\"\""

REM Check if running as admin
net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Please run as Administrator!
    echo Right-click this script and select "Run as administrator"
    pause
    exit /b 1
)

REM Check if startup script exists
if not exist "%SCRIPT_PATH%" (
    echo [ERROR] Startup script not found: %SCRIPT_PATH%
    pause
    exit /b 1
)

REM Check if local config exists
if not exist "%CONFIG_PATH%" (
    echo [ERROR] Cloud service config not found: %CONFIG_PATH%
    echo Copy %CONFIG_EXAMPLE% to %CONFIG_PATH% and fill in VCI_PROXY_AUTH_TOKEN first.
    pause
    exit /b 1
)

REM Register scheduled task to run at system startup
echo Registering auto-start task...
schtasks /create /tn "%TASK_NAME%" /tr "%TASK_CMD%" /sc onstart /delay %START_DELAY% /ru SYSTEM /rl HIGHEST /f

if errorlevel 1 (
    echo [ERROR] Failed to create scheduled task!
    pause
    exit /b 1
)

echo.
echo [OK] Auto-start registered successfully!
echo.
echo  Task name:  %TASK_NAME%
echo  Trigger:    At system startup
echo  Delay:      %START_DELAY%
echo  Action:     %TASK_CMD%
echo  Run as:     SYSTEM (highest privileges)
echo.
echo  Note:       Session 0 startup only launches headless services.
echo              GDS2 is skipped automatically in this context.
echo              Secrets are loaded from %CONFIG_PATH%
echo.
echo  To verify:  schtasks /query /tn "%TASK_NAME%"
echo  To remove:  schtasks /delete /tn "%TASK_NAME%" /f
echo.
pause
