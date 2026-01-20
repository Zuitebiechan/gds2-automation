@echo off
chcp 65001 >nul
echo ================================================
echo GDS2 UI Inspector
echo ================================================
echo.
echo Make sure GDS2 is running before continuing.
echo.
pause

call venv\Scripts\activate.bat
python main.py inspect
pause
