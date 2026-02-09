@echo off
REM Install VCI Proxy as Windows services using NSSM
REM Requires: NSSM (https://nssm.cc) in PATH
REM Must run as Administrator
REM
REM Usage: install_service.bat [server_host] [server_port]

setlocal

set SERVER_HOST=%~1
set SERVER_PORT=%~2
if "%SERVER_HOST%"=="" set SERVER_HOST=8.136.197.36
if "%SERVER_PORT%"=="" set SERVER_PORT=9000

REM Check admin privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: This script must be run as Administrator.
    echo Right-click and select "Run as administrator".
    pause
    exit /b 1
)

REM Check NSSM is available
where nssm >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: NSSM not found in PATH.
    echo Download from https://nssm.cc and add to PATH.
    pause
    exit /b 1
)

REM Find Python
for /f "tokens=*" %%i in ('where python 2^>nul') do set PYTHON_PATH=%%i
if "%PYTHON_PATH%"=="" (
    echo ERROR: Python not found in PATH.
    pause
    exit /b 1
)

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..\..
set LOG_DIR=%USERPROFILE%\gds2-data

REM Ensure log directory exists
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo ============================================
echo  VCI Proxy - Installing Windows Services
echo ============================================
echo  Python:      %PYTHON_PATH%
echo  Project dir: %PROJECT_DIR%
echo  Server host: %SERVER_HOST%
echo  Server port: %SERVER_PORT%
echo  Log dir:     %LOG_DIR%
echo ============================================
echo.

REM Remove existing services (if any)
nssm stop VCI-Proxy-Server >nul 2>&1
nssm remove VCI-Proxy-Server confirm >nul 2>&1
nssm stop VCI-Proxy-Client >nul 2>&1
nssm remove VCI-Proxy-Client confirm >nul 2>&1

REM Install VCI-Proxy-Server
echo Installing VCI-Proxy-Server...
nssm install VCI-Proxy-Server "%PYTHON_PATH%" -m vci_proxy.reverse_server --listen-port %SERVER_PORT% --proxy-port 9001
nssm set VCI-Proxy-Server AppDirectory "%PROJECT_DIR%"
nssm set VCI-Proxy-Server DisplayName "VCI Proxy Server"
nssm set VCI-Proxy-Server Description "VCI Proxy reverse connection server - accepts VCI client connections"
nssm set VCI-Proxy-Server Start SERVICE_AUTO_START
nssm set VCI-Proxy-Server AppStdout "%LOG_DIR%\vci_proxy_server.log"
nssm set VCI-Proxy-Server AppStderr "%LOG_DIR%\vci_proxy_server.log"
nssm set VCI-Proxy-Server AppRotateFiles 1
nssm set VCI-Proxy-Server AppRotateBytes 10485760
nssm set VCI-Proxy-Server AppRotateOnline 1

REM Install VCI-Proxy-Client
echo Installing VCI-Proxy-Client...
nssm install VCI-Proxy-Client "%PYTHON_PATH%" -m vci_proxy.reverse_client --host %SERVER_HOST% --port %SERVER_PORT%
nssm set VCI-Proxy-Client AppDirectory "%PROJECT_DIR%"
nssm set VCI-Proxy-Client DisplayName "VCI Proxy Client"
nssm set VCI-Proxy-Client Description "VCI Proxy reverse connection client - connects to remote server"
nssm set VCI-Proxy-Client Start SERVICE_AUTO_START
nssm set VCI-Proxy-Client AppStdout "%LOG_DIR%\vci_proxy_client.log"
nssm set VCI-Proxy-Client AppStderr "%LOG_DIR%\vci_proxy_client.log"
nssm set VCI-Proxy-Client AppRotateFiles 1
nssm set VCI-Proxy-Client AppRotateBytes 10485760
nssm set VCI-Proxy-Client AppRotateOnline 1

echo.
echo ============================================
echo  Services installed successfully!
echo ============================================
echo.
echo Commands:
echo   nssm start VCI-Proxy-Server
echo   nssm start VCI-Proxy-Client
echo   nssm stop VCI-Proxy-Server
echo   nssm stop VCI-Proxy-Client
echo   nssm status VCI-Proxy-Server
echo   nssm status VCI-Proxy-Client
echo.

REM Start services
echo Starting services...
nssm start VCI-Proxy-Server
timeout /t 2 /nobreak > nul
nssm start VCI-Proxy-Client

echo.
echo Done. Services are running and will auto-start on boot.

endlocal
pause
