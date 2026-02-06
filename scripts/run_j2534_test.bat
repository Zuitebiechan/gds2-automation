@echo off
chcp 65001 >nul
echo === J2534 本地测试 ===
echo.

REM 查找 32 位 Python
set PYTHON32=

if exist "C:\Python311-32\python.exe" (
    set PYTHON32=C:\Python311-32\python.exe
    goto :found
)

if exist "C:\Python310-32\python.exe" (
    set PYTHON32=C:\Python310-32\python.exe
    goto :found
)

if exist "%LOCALAPPDATA%\Programs\Python\Python311-32\python.exe" (
    set PYTHON32=%LOCALAPPDATA%\Programs\Python\Python311-32\python.exe
    goto :found
)

echo 未找到 32 位 Python！
echo.
echo 请先运行以下命令安装 32 位 Python：
echo   powershell -ExecutionPolicy Bypass -File scripts\setup_python32.ps1
echo.
echo 或者手动下载安装：
echo   https://www.python.org/ftp/python/3.11.9/python-3.11.9.exe
echo.
pause
exit /b 1

:found
echo 使用 Python: %PYTHON32%
echo.

REM 切换到项目根目录
cd /d "%~dp0.."

REM 运行测试
%PYTHON32% scripts\test_j2534_local.py

pause
