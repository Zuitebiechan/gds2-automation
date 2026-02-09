# Pack Cloud Web UI files into a zip with proper directory structure
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

Set-Location $PSScriptRoot\..
$projectDir = Get-Location

$files = @(
    'app.py',
    'main.py',
    'requirements-cloud.txt',
    'templates\index.html',
    'src\__init__.py',
    'src\core\__init__.py',
    'src\discovery\__init__.py',
    'src\discovery\vehicle_mapping.py',
    'src\native\__init__.py',
    'src\native\device_explorer.py',
    'src\navigation\__init__.py',
    'src\navigation\controller.py',
    'src\streaming\__init__.py',
    'src\streaming\agent_navigator.py',
    'src\streaming\agent_data_collector.py',
    'src\utils\__init__.py',
    'src\utils\report_parser.py',
    'src\workflows\__init__.py',
    'src\workflows\data_viewer.py',
    'src\workflows\interactive_workflow.py',
    'src\workflows\read_data_display_agent.py',
    'scripts\cloud_start_webui.bat'
)

$output = Join-Path $projectDir 'cloud_webui.zip'

if (Test-Path $output) { Remove-Item $output }

# Verify all files exist
$missing = @()
foreach ($f in $files) {
    if (-not (Test-Path $f)) {
        $missing += $f
    }
}

if ($missing.Count -gt 0) {
    Write-Host "ERROR: Missing files:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 1
}

# Create zip with proper directory structure
$zip = [System.IO.Compression.ZipFile]::Open($output, [System.IO.Compression.ZipArchiveMode]::Create)

foreach ($f in $files) {
    $fullPath = Join-Path $projectDir $f
    # Use forward slashes in zip entry name
    $entryName = $f.Replace('\', '/')
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $zip, $fullPath, $entryName,
        [System.IO.Compression.CompressionLevel]::Fastest
    ) | Out-Null
    Write-Host "  + $entryName"
}

$zip.Dispose()

$sz = (Get-Item $output).Length
Write-Host ""
Write-Host "Created cloud_webui.zip: $([math]::Round($sz/1024, 1)) KB" -ForegroundColor Green
Write-Host "Files included: $($files.Count)" -ForegroundColor Cyan
Write-Host ""
Write-Host "Deployment steps:" -ForegroundColor Yellow
Write-Host "  1. Upload cloud_webui.zip to the cloud server"
Write-Host "  2. Extract: powershell Expand-Archive cloud_webui.zip -DestinationPath C:\RPA_demo"
Write-Host "  3. cd C:\RPA_demo && pip install -r requirements-cloud.txt"
Write-Host "  4. scripts\cloud_start_webui.bat"
