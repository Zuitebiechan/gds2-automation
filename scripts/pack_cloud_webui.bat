@echo off
REM Pack Web UI files for cloud deployment
REM Creates cloud_webui.zip with all necessary files

setlocal

set PROJECT_DIR=%~dp0..
set OUTPUT=%PROJECT_DIR%\cloud_webui.zip

echo ============================================
echo  Pack Cloud Web UI Deployment
echo ============================================

REM Delete old zip if exists
if exist "%OUTPUT%" del "%OUTPUT%"

cd /d "%PROJECT_DIR%"

REM Use PowerShell to create zip with specific files
powershell -NoProfile -Command ^
  "$files = @(" ^
    "'app.py'," ^
    "'main.py'," ^
    "'requirements-cloud.txt'," ^
    "'templates\index.html'," ^
    "'src\__init__.py'," ^
    "'src\core\__init__.py'," ^
    "'src\discovery\__init__.py'," ^
    "'src\discovery\vehicle_mapping.py'," ^
    "'src\native\__init__.py'," ^
    "'src\native\device_explorer.py'," ^
    "'src\navigation\__init__.py'," ^
    "'src\navigation\controller.py'," ^
    "'src\streaming\__init__.py'," ^
    "'src\streaming\agent_navigator.py'," ^
    "'src\streaming\agent_data_collector.py'," ^
    "'src\utils\__init__.py'," ^
    "'src\utils\report_parser.py'," ^
    "'src\workflows\__init__.py'," ^
    "'src\workflows\data_viewer.py'," ^
    "'src\workflows\interactive_workflow.py'," ^
    "'src\workflows\read_data_display_agent.py'," ^
    "'scripts\cloud_start_webui.bat'" ^
  ");" ^
  "$compress = @{" ^
    "Path = $files;" ^
    "DestinationPath = 'cloud_webui.zip';" ^
    "CompressionLevel = 'Fastest'" ^
  "};" ^
  "Compress-Archive @compress;" ^
  "Write-Host ('Created cloud_webui.zip: ' + (Get-Item 'cloud_webui.zip').Length / 1KB + ' KB')"

echo.
echo Done! Upload cloud_webui.zip to the cloud server and extract.
echo Then run: pip install -r requirements-cloud.txt
echo Then run: python main.py web --port 8080
echo.

endlocal
