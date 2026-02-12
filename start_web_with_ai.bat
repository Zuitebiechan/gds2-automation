@echo off
REM 启动带 AI recovery 的 Web UI

echo ========================================
echo 启动 GDS2 Web UI (带 AI 恢复功能)
echo ========================================
echo.
echo 配置检查...
python verify_config.py
echo.
echo ========================================
echo 启动 Web UI...
echo 访问: http://localhost:8080
echo.
python main.py web
