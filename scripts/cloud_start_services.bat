@echo off
REM ============================================================
REM  Cloud Services Startup Script
REM  Starts the required cloud-side services for the diagnostics
REM  platform:
REM    1. VCI Proxy reverse server
REM    2. Flask API server
REM    3. Optional GDS2 + Java Agent launcher
REM
REM  Usage:
REM    scripts\cloud_start_services.bat
REM    scripts\cloud_start_services.bat --no-gds2
REM
REM  Secrets/config:
REM    - optional local config: scripts\cloud_service_config.cmd
REM    - required after config/env load: VCI_PROXY_AUTH_TOKEN
REM ============================================================

setlocal EnableExtensions

for %%I in ("%~dp0..") do set "DEFAULT_PROJECT_DIR=%%~fI"
if not defined PROJECT_DIR set "PROJECT_DIR=%DEFAULT_PROJECT_DIR%"

set "CONFIG_FILE=%~dp0cloud_service_config.cmd"
if exist "%CONFIG_FILE%" (
    call "%CONFIG_FILE%"
    if errorlevel 1 (
        echo [ERROR] Failed to load config file: %CONFIG_FILE%
        exit /b 1
    )
)

if not defined PROJECT_DIR set "PROJECT_DIR=%DEFAULT_PROJECT_DIR%"
if not defined GDS2_AGENT_DIR set "GDS2_AGENT_DIR=C:\tools\gds2-agent"
if not defined LOG_DIR set "LOG_DIR=%PROJECT_DIR%\logs"
if not defined DIAGNOSTIC_API_PORT set "DIAGNOSTIC_API_PORT=8080"
if not defined VCI_PROXY_PORT set "VCI_PROXY_PORT=9000"
if not defined VCI_PROXY_LOCAL_PROXY_PORT set "VCI_PROXY_LOCAL_PROXY_PORT=9001"
if not defined DIAGNOSTIC_API_PUBLIC set "DIAGNOSTIC_API_PUBLIC=0"
if not defined DIAGNOSTIC_API_ENABLE_CORS set "DIAGNOSTIC_API_ENABLE_CORS=0"
if not defined VCI_PROXY_TLS_ENABLED set "VCI_PROXY_TLS_ENABLED=0"
if not defined VCI_PROXY_TLS_REQUIRE_CLIENT_CERT set "VCI_PROXY_TLS_REQUIRE_CLIENT_CERT=0"

set "SKIP_GDS2=0"
if not defined PYTHON_EXE set "PYTHON_EXE="

if /I "%~1"=="--no-gds2" set "SKIP_GDS2=1"

REM Detect Session 0 (scheduled task / SYSTEM context).
if not defined SESSIONNAME set "SKIP_GDS2=1"
if "%SESSIONNAME%"=="" set "SKIP_GDS2=1"
if /I "%SESSIONNAME%"=="Services" set "SKIP_GDS2=1"

call :resolve_python

echo ============================================================
echo  Diagnostic Platform - Cloud Services
echo ============================================================
echo.
echo  Project dir: %PROJECT_DIR%
if exist "%CONFIG_FILE%" (
    echo  Config:      %CONFIG_FILE%
) else (
    echo  Config:      [not found, using current environment]
)
if defined PYTHON_EXE (
    echo  Python:      %PYTHON_EXE%
) else (
    echo  Python:      [not found]
)
echo.

if not defined PYTHON_EXE goto :python_missing
if not defined VCI_PROXY_AUTH_TOKEN goto :auth_missing
if /I "%VCI_PROXY_TLS_ENABLED%"=="1" if not defined VCI_PROXY_TLS_CERT goto :tls_cert_missing
if /I "%VCI_PROXY_TLS_ENABLED%"=="1" if not defined VCI_PROXY_TLS_KEY goto :tls_key_missing

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

call :start_proxy_server
call :start_flask_api
call :start_gds2

echo.
echo ============================================================
echo  All services started!
echo ============================================================
echo.
echo  VCI Proxy:  ports %VCI_PROXY_PORT%/%VCI_PROXY_LOCAL_PROXY_PORT%  (log: %LOG_DIR%\vci_proxy.log)
echo  Flask API:  port %DIAGNOSTIC_API_PORT%  (log: %LOG_DIR%\flask_api.log)
if "%SKIP_GDS2%"=="0" echo  GDS2:       running with agent
echo.
echo  To stop all services:
echo    taskkill /FI "WINDOWTITLE eq VCI-Proxy-Server" /F
echo    taskkill /FI "WINDOWTITLE eq Flask-API" /F
echo ============================================================
exit /b 0

:python_missing
echo [ERROR] Python not found.
echo Checked:
echo   - %%PYTHON%% env var
echo   - %%PYTHON_EXE%% env var
echo   - %PROJECT_DIR%\venv32\Scripts\python.exe
echo   - %PROJECT_DIR%\venv\Scripts\python.exe
echo   - py -3 ^(resolved to sys.executable^)
echo   - python on PATH ^(resolved to sys.executable^)
exit /b 1

:auth_missing
echo [ERROR] VCI_PROXY_AUTH_TOKEN is not configured.
echo Create scripts\cloud_service_config.cmd from
echo scripts\cloud_service_config.example.cmd, then fill in the shared token.
echo You can also export VCI_PROXY_AUTH_TOKEN in the environment before running this script.
exit /b 1

:tls_cert_missing
echo [ERROR] VCI_PROXY_TLS_ENABLED=1 but VCI_PROXY_TLS_CERT is not set.
exit /b 1

:tls_key_missing
echo [ERROR] VCI_PROXY_TLS_ENABLED=1 but VCI_PROXY_TLS_KEY is not set.
exit /b 1

:resolve_python
if defined PYTHON_EXE if exist "%PYTHON_EXE%" goto :eof
if defined PYTHON_EXE set "PYTHON_EXE="
if not defined PYTHON_EXE if defined PYTHON if exist "%PYTHON%" set "PYTHON_EXE=%PYTHON%"
if defined PYTHON_EXE goto :eof
if exist "%PROJECT_DIR%\venv32\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%\venv32\Scripts\python.exe"
if defined PYTHON_EXE goto :eof
if exist "%PROJECT_DIR%\venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%\venv\Scripts\python.exe"
if defined PYTHON_EXE goto :eof
for /f "usebackq delims=" %%I in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do (
    if not defined PYTHON_EXE set "PYTHON_EXE=%%I"
)
if defined PYTHON_EXE goto :eof
for /f "usebackq delims=" %%I in (`python -c "import sys; print(sys.executable)" 2^>nul`) do (
    if not defined PYTHON_EXE set "PYTHON_EXE=%%I"
)
goto :eof

:start_proxy_server
powershell -NoProfile -Command "$ready = Test-NetConnection -ComputerName '127.0.0.1' -Port %VCI_PROXY_PORT% -InformationLevel Quiet -WarningAction SilentlyContinue; if ($ready) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    echo [1/3] VCI Proxy reverse server already listening on port %VCI_PROXY_PORT%, skipping start.
    goto :eof
)

set "VCI_PROXY_TLS_ARGS="
if /I "%VCI_PROXY_TLS_ENABLED%"=="1" set "VCI_PROXY_TLS_ARGS=%VCI_PROXY_TLS_ARGS% --tls"
if /I "%VCI_PROXY_TLS_ENABLED%"=="1" if defined VCI_PROXY_TLS_CERT set "VCI_PROXY_TLS_ARGS=%VCI_PROXY_TLS_ARGS% --tls-cert ""%VCI_PROXY_TLS_CERT%"""
if /I "%VCI_PROXY_TLS_ENABLED%"=="1" if defined VCI_PROXY_TLS_KEY set "VCI_PROXY_TLS_ARGS=%VCI_PROXY_TLS_ARGS% --tls-key ""%VCI_PROXY_TLS_KEY%"""
if /I "%VCI_PROXY_TLS_ENABLED%"=="1" if defined VCI_PROXY_TLS_CA set "VCI_PROXY_TLS_ARGS=%VCI_PROXY_TLS_ARGS% --tls-ca ""%VCI_PROXY_TLS_CA%"""
if /I "%VCI_PROXY_TLS_ENABLED%"=="1" if /I "%VCI_PROXY_TLS_REQUIRE_CLIENT_CERT%"=="1" set "VCI_PROXY_TLS_ARGS=%VCI_PROXY_TLS_ARGS% --tls-require-client-cert"

echo [1/3] Starting VCI Proxy reverse server...
start "VCI-Proxy-Server" /D "%PROJECT_DIR%" /MIN "%ComSpec%" /c ""%PYTHON_EXE%" -m vci_proxy.reverse_server --vci-port %VCI_PROXY_PORT% --proxy-port %VCI_PROXY_LOCAL_PROXY_PORT% --auth-token "%VCI_PROXY_AUTH_TOKEN%"%VCI_PROXY_TLS_ARGS% > "%LOG_DIR%\vci_proxy.log" 2>&1"
call :wait_for_port 127.0.0.1 %VCI_PROXY_PORT% 15
if errorlevel 1 (
    echo       [WARN] Port %VCI_PROXY_PORT% did not become ready in time. Check %LOG_DIR%\vci_proxy.log
    goto :eof
)
echo       Listening on ports %VCI_PROXY_PORT% (VCI) and %VCI_PROXY_LOCAL_PROXY_PORT% (proxy)
goto :eof

:start_flask_api
powershell -NoProfile -Command "$ready = Test-NetConnection -ComputerName '127.0.0.1' -Port %DIAGNOSTIC_API_PORT% -InformationLevel Quiet -WarningAction SilentlyContinue; if ($ready) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    echo [2/3] Flask API server already listening on port %DIAGNOSTIC_API_PORT%, skipping start.
    goto :eof
)

set "FLASK_ARGS=app.py --port %DIAGNOSTIC_API_PORT%"
if /I "%DIAGNOSTIC_API_PUBLIC%"=="1" set "FLASK_ARGS=%FLASK_ARGS% --public"
if /I "%DIAGNOSTIC_API_ENABLE_CORS%"=="1" set "FLASK_ARGS=%FLASK_ARGS% --cors"

echo [2/3] Starting Flask API server...
start "Flask-API" /D "%PROJECT_DIR%" /MIN "%ComSpec%" /c ""%PYTHON_EXE%" %FLASK_ARGS% > "%LOG_DIR%\flask_api.log" 2>&1"
call :wait_for_port 127.0.0.1 %DIAGNOSTIC_API_PORT% 15
if errorlevel 1 (
    echo       [WARN] Port %DIAGNOSTIC_API_PORT% did not become ready in time. Check %LOG_DIR%\flask_api.log
    goto :eof
)
echo       Listening on port %DIAGNOSTIC_API_PORT%
goto :eof

:start_gds2
if "%SKIP_GDS2%"=="1" (
    echo [3/3] Skipping GDS2 launch
    goto :eof
)
if not exist "%GDS2_AGENT_DIR%\launch-gds2-with-agent.bat" (
    echo [3/3] GDS2 agent launcher not found, skipping
    goto :eof
)
echo [3/3] Starting GDS2 with Java Agent...
start "GDS2" cmd /c """%GDS2_AGENT_DIR%\launch-gds2-with-agent.bat"""
timeout /t 3 /nobreak >nul
echo       GDS2 launched with agent (interval=100ms)
goto :eof

:wait_for_port
setlocal EnableDelayedExpansion
set "WAIT_HOST=%~1"
set "WAIT_PORT=%~2"
set "WAIT_RETRIES=%~3"
for /l %%I in (1,1,!WAIT_RETRIES!) do (
    powershell -NoProfile -Command "$ready = Test-NetConnection -ComputerName '!WAIT_HOST!' -Port !WAIT_PORT! -InformationLevel Quiet -WarningAction SilentlyContinue; if ($ready) { exit 0 } else { exit 1 }" >nul 2>&1
    if not errorlevel 1 (
        endlocal
        exit /b 0
    )
    timeout /t 2 /nobreak >nul
)
endlocal
exit /b 1
