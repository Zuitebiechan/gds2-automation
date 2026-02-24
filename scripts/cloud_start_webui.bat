@echo off
REM Start GDS2 Web UI on cloud server
REM Run this from the RPA_demo project directory

setlocal

set PORT=8080

REM Parse arguments
:parse_args
if "%~1"=="" goto :done_args
if "%~1"=="--port" (
    set PORT=%~2
    shift
    shift
    goto :parse_args
)
shift
goto :parse_args
:done_args

echo ============================================
echo  GDS2 Web UI - Cloud Server
echo ============================================
echo  Port: %PORT%
echo  Access: http://^<server-ip^>:%PORT%
echo  Press Ctrl+C to stop
echo ============================================

REM Check if flask is installed
python -c "import flask" 2>nul
if errorlevel 1 (
    echo.
    echo Flask not found. Installing dependencies...
    pip install -r requirements-cloud.txt
    echo.
)

REM Start the web UI
if not exist app.py (
    echo [ERROR] app.py not found in current directory.
    echo Run this script from the project root folder.
    exit /b 1
)

python app.py --port %PORT%

endlocal
