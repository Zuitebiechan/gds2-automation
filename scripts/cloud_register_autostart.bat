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

echo ============================================================
echo  Register Auto-Start Services
echo ============================================================
echo.

set "PROJECT_DIR=C:\gds2-automation"
set "SCRIPT_PATH=%PROJECT_DIR%\scripts\cloud_start_services.bat"

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

REM Register scheduled task to run at system startup
echo Registering auto-start task...
schtasks /create /tn "DiagPlatform-AutoStart" /tr "\"%SCRIPT_PATH%\"" /sc onstart /ru SYSTEM /rl HIGHEST /f

if errorlevel 1 (
    echo [ERROR] Failed to create scheduled task!
    pause
    exit /b 1
)

echo.
echo [OK] Auto-start registered successfully!
echo.
echo  Task name:  DiagPlatform-AutoStart
echo  Trigger:    At system startup
echo  Action:     %SCRIPT_PATH%
echo  Run as:     SYSTEM (highest privileges)
echo.
echo  To verify:  schtasks /query /tn "DiagPlatform-AutoStart"
echo  To remove:  schtasks /delete /tn "DiagPlatform-AutoStart" /f
echo.
pause
