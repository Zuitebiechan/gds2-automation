@echo off
REM Setup GDS2 Agent + Desktop Shortcut on Cloud Server
REM Run this script on the cloud server after copying files

setlocal enabledelayedexpansion

set "AGENT_DIR=C:\tools\gds2-agent"
set "GDS2_DIR=C:\Program Files (x86)\GDS 2"
set "DESKTOP=%USERPROFILE%\Desktop"

echo ============================================
echo  GDS2 Agent - Cloud Server Setup
echo ============================================
echo.

REM Step 1: Check GDS2 installation
if not exist "!GDS2_DIR!\jre6\bin\javaw.exe" goto :no_gds2
echo [OK] GDS2 found at: !GDS2_DIR!

REM Step 2: Check agent JAR
if not exist "!AGENT_DIR!\gds2-agent.jar" goto :no_jar
echo [OK] Agent JAR found: !AGENT_DIR!\gds2-agent.jar

REM Step 3: Check launch script
if not exist "!AGENT_DIR!\launch-gds2-with-agent.bat" goto :no_launch
echo [OK] Launch script found

REM Step 4: Create gds2-data output directory
if not exist "%USERPROFILE%\gds2-data" mkdir "%USERPROFILE%\gds2-data"
echo [OK] Data output dir: %USERPROFILE%\gds2-data

REM Step 5: Create desktop shortcut using PowerShell
echo.
echo Creating desktop shortcut...

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1" "!AGENT_DIR!" "!GDS2_DIR!" "!DESKTOP!"
if errorlevel 1 goto :shortcut_fail

echo.
echo ============================================
echo  Setup Complete!
echo ============================================
echo.
echo  Desktop shortcut: GDS2 (Agent)
echo  Agent JAR: !AGENT_DIR!\gds2-agent.jar
echo  Data output: %USERPROFILE%\gds2-data
echo  Log file: %USERPROFILE%\gds2-agent.log
echo.
echo  Double-click "GDS2 (Agent)" on Desktop to start.
echo ============================================
echo.
pause
goto :eof

:no_gds2
echo [ERROR] GDS2 not found at: !GDS2_DIR!
echo Please install GDS2 first.
echo.
pause
goto :eof

:no_jar
echo [ERROR] Agent JAR not found at: !AGENT_DIR!\gds2-agent.jar
echo Please copy gds2-agent.jar to !AGENT_DIR!\
echo.
pause
goto :eof

:no_launch
echo [ERROR] launch-gds2-with-agent.bat not found at: !AGENT_DIR!
echo Please copy launch-gds2-with-agent.bat to !AGENT_DIR!\
echo.
pause
goto :eof

:shortcut_fail
echo [ERROR] Failed to create desktop shortcut.
echo Please check PowerShell is available.
echo.
pause
goto :eof
