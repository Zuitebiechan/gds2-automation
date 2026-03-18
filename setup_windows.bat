@echo off
chcp 65001 >nul
echo ================================================
echo GDS2 RPA Demo - Windows Setup
echo ================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.8+
    echo Download from: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [OK] Python found:
python --version
echo.

:: Create virtual environment
echo Creating virtual environment...
if not exist "venv32" (
    python -m venv venv32
)
echo [OK] Virtual environment ready
echo.

:: Activate and install dependencies
echo Installing dependencies...
call venv32\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-minimal.txt

echo.
echo ================================================
echo Setup complete!
echo ================================================
echo.
echo To run the demo:
echo   1. Open Command Prompt in this folder
echo   2. Run: venv32\Scripts\activate
echo   3. Run: python main.py inspect
echo.
pause
