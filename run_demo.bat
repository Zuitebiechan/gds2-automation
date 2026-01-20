@echo off
chcp 65001 >nul
echo ================================================
echo GDS2 RPA Demo
echo ================================================
echo.

call venv\Scripts\activate.bat
python main.py demo
pause
