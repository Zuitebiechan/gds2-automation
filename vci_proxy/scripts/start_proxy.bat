@echo off
REM Start VCI Proxy (reverse_server + reverse_client)
REM Usage: start_proxy.bat [server_host] [server_port]
REM   Default host: 8.136.197.36
REM   Default port: 9000

setlocal

set SERVER_HOST=%~1
set SERVER_PORT=%~2
if "%SERVER_HOST%"=="" set SERVER_HOST=8.136.197.36
if "%SERVER_PORT%"=="" set SERVER_PORT=9000

set SCRIPT_DIR=%~dp0
set PROXY_DIR=%SCRIPT_DIR%..

echo ============================================
echo  VCI Proxy - Starting services
echo ============================================
echo  Server host: %SERVER_HOST%
echo  Server port: %SERVER_PORT%
echo ============================================

REM Start reverse_server in a minimized window
echo Starting reverse_server...
start "VCI-Proxy-Server" /min cmd /c "cd /d "%PROXY_DIR%\.." && python -m vci_proxy.reverse_server --listen-port %SERVER_PORT% --proxy-port 9001"

REM Wait for server to start
timeout /t 2 /nobreak > nul

REM Start reverse_client in a minimized window
echo Starting reverse_client...
start "VCI-Proxy-Client" /min cmd /c "cd /d "%PROXY_DIR%\.." && python -m vci_proxy.reverse_client --host %SERVER_HOST% --port %SERVER_PORT%"

echo.
echo Both services started.
echo  - reverse_server: window title "VCI-Proxy-Server"
echo  - reverse_client: window title "VCI-Proxy-Client"
echo.
echo Use stop_proxy.bat to stop both services.

endlocal
