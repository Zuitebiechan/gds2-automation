# Create desktop shortcut for GDS2 (Agent)
# Called by cloud_setup_gds2_agent.bat
param(
    [string]$AgentDir,
    [string]$GDS2Dir,
    [string]$Desktop
)

$shortcutPath = Join-Path $Desktop "GDS2 (Agent).lnk"
$targetPath = Join-Path $AgentDir "launch-gds2-with-agent.bat"
$iconPath = Join-Path $GDS2Dir "bin\GDS2Launcher.exe"

$ws = New-Object -ComObject WScript.Shell
$shortcut = $ws.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $targetPath
$shortcut.WorkingDirectory = $AgentDir
$shortcut.Description = "GDS2 with Java Data Extraction Agent"
$shortcut.IconLocation = "$iconPath,0"
$shortcut.Save()

Write-Host "[OK] Shortcut created: $shortcutPath"
