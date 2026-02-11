# Setup 32-bit Python for J2534 testing
# Run this in PowerShell as Administrator

Write-Host "=== 32位 Python 安装脚本 ===" -ForegroundColor Cyan
Write-Host ""

# Check if 32-bit Python exists
$python32Paths = @(
    "C:\Python311-32\python.exe",
    "C:\Python310-32\python.exe",
    "C:\Python39-32\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311-32\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310-32\python.exe"
)

$existingPython32 = $null
foreach ($path in $python32Paths) {
    if (Test-Path $path) {
        $existingPython32 = $path
        break
    }
}

if ($existingPython32) {
    Write-Host "找到 32 位 Python: $existingPython32" -ForegroundColor Green
    & $existingPython32 -c "import struct; print(f'架构: {struct.calcsize(\"P\") * 8} 位')"
    Write-Host ""
    Write-Host "可以直接运行测试:" -ForegroundColor Yellow
    Write-Host "  $existingPython32 scripts\test_j2534_local.py"
    exit 0
}

Write-Host "未找到 32 位 Python，正在下载..." -ForegroundColor Yellow

# Download Python 3.11 32-bit
$installerUrl = "https://www.python.org/ftp/python/3.11.9/python-3.11.9.exe"
$installerPath = "$env:TEMP\python-3.11.9-x86.exe"

try {
    Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing
    Write-Host "下载完成: $installerPath" -ForegroundColor Green
} catch {
    Write-Host "下载失败: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "请手动下载 32 位 Python:" -ForegroundColor Yellow
    Write-Host "  https://www.python.org/ftp/python/3.11.9/python-3.11.9.exe"
    Write-Host ""
    Write-Host "安装时请：" -ForegroundColor Yellow
    Write-Host "  1. 选择 'Customize installation'"
    Write-Host "  2. 安装路径设为: C:\Python311-32"
    Write-Host "  3. 勾选 'Add Python to PATH' (可选)"
    exit 1
}

Write-Host ""
Write-Host "开始安装 Python 3.11 (32位)..." -ForegroundColor Cyan
Write-Host "安装路径: C:\Python311-32" -ForegroundColor Cyan
Write-Host ""

# Install silently to C:\Python311-32
Start-Process -FilePath $installerPath -ArgumentList "/quiet", "InstallAllUsers=0", "TargetDir=C:\Python311-32", "PrependPath=0" -Wait

if (Test-Path "C:\Python311-32\python.exe") {
    Write-Host "安装成功!" -ForegroundColor Green
    Write-Host ""
    Write-Host "运行测试:" -ForegroundColor Yellow
    Write-Host "  C:\Python311-32\python.exe scripts\test_j2534_local.py"
} else {
    Write-Host "安装可能失败，请手动安装" -ForegroundColor Red
}

# Cleanup
Remove-Item $installerPath -ErrorAction SilentlyContinue
