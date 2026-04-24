@echo off
REM Copy this file to scripts\cloud_service_config.cmd on the cloud server.
REM Fill in the real values there. Do not commit the copied file.

set "PROJECT_DIR=C:\path\to\RPA_demo"
set "PYTHON=%PROJECT_DIR%\venv\Scripts\python.exe"
set "LOG_DIR=%PROJECT_DIR%\logs"
set "GDS2_AGENT_DIR=C:\tools\gds2-agent"

REM Required for reverse tunnel authentication.
set "VCI_PROXY_AUTH_TOKEN=replace-with-shared-token"

REM Optional API auth. Leave blank to disable API token enforcement.
set "DIAGNOSTIC_API_TOKEN="

REM Optional API listener settings.
set "DIAGNOSTIC_API_PORT=8080"
set "DIAGNOSTIC_API_PUBLIC=0"
set "DIAGNOSTIC_API_ENABLE_CORS=0"
set "DIAGNOSTIC_API_CORS_ORIGINS="

REM Optional cloud observability root override.
REM Example: D:\RPA_Diagnostic\observability\cloud
set "PRODUCT_LOG_CLOUD_ROOT="

REM Optional reverse-server port overrides.
set "VCI_PROXY_PORT=9000"
set "VCI_PROXY_LOCAL_PROXY_PORT=9001"

REM Optional TLS settings for the reverse tunnel.
set "VCI_PROXY_TLS_ENABLED=0"
set "VCI_PROXY_TLS_CERT=C:\secrets\vci_proxy_server.crt"
set "VCI_PROXY_TLS_KEY=C:\secrets\vci_proxy_server.key"
set "VCI_PROXY_TLS_CA="
set "VCI_PROXY_TLS_REQUIRE_CLIENT_CERT=0"
