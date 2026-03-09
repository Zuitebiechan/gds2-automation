"""Create desktop shortcut for GDS2 with Java Agent."""
import subprocess
import sys

ps_script = r"""
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$sc = $ws.CreateShortcut("$desktop\GDS2 (with Agent).lnk")
$sc.TargetPath = 'C:\tools\gds2-agent\launch-gds2-with-agent.bat'
$sc.WorkingDirectory = 'C:\Program Files (x86)\GDS 2\bin'
$sc.Description = 'Launch GDS2 with Java Agent for RPA automation'
$sc.IconLocation = 'C:\Program Files (x86)\GDS 2\bin\GDS2Launcher.exe, 0'
$sc.Save()
Write-Host "Shortcut created at $desktop\GDS2 (with Agent).lnk"
"""

result = subprocess.run(
    ["powershell", "-NoProfile", "-Command", ps_script],
    capture_output=True, text=True
)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr, file=sys.stderr)
sys.exit(result.returncode)
