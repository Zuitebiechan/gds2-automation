@echo off
REM ============================================================
REM  Unregister Local Zone edge proxy auto-start
REM ============================================================

setlocal EnableExtensions
set "TASK_NAME=DiagPlatform-EdgeProxy"

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Please run as Administrator.
    exit /b 1
)

echo Removing task %TASK_NAME%...
schtasks /delete /tn "%TASK_NAME%" /f
if errorlevel 1 (
    echo [ERROR] Failed to delete scheduled task.
    exit /b 1
)

echo [OK] Removed task %TASK_NAME%
exit /b 0
