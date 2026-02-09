@echo off
REM Stop VCI Proxy services
REM Kills by window title, fallback by process command line

setlocal

echo ============================================
echo  VCI Proxy - Stopping services
echo ============================================

REM Kill by window title
taskkill /fi "WINDOWTITLE eq VCI-Proxy-Server" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq VCI-Proxy-Client" /f >nul 2>&1

REM Fallback: kill Python processes running reverse_server or reverse_client
for /f "tokens=1" %%i in ('wmic process where "commandline like '%%reverse_server%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /pid %%i /f >nul 2>&1
)
for /f "tokens=1" %%i in ('wmic process where "commandline like '%%reverse_client%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /pid %%i /f >nul 2>&1
)

echo Services stopped.

endlocal
