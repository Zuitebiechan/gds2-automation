@echo off
REM ============================================================
REM  Unregister Cloud Services Auto-Start Task
REM ============================================================

setlocal

echo ============================================================
echo  Unregister Auto-Start Services
echo ============================================================
echo.

schtasks /query /tn "DiagPlatform-AutoStart" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Task "DiagPlatform-AutoStart" was not found.
    echo.
    pause
    exit /b 0
)

echo Removing scheduled task...
schtasks /delete /tn "DiagPlatform-AutoStart" /f

if errorlevel 1 (
    echo [ERROR] Failed to delete scheduled task.
    echo.
    pause
    exit /b 1
)

echo.
echo [OK] Task removed successfully.
echo.
pause
