param(
    [string]$SessionName = "omx-rpa-demo"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

# Refresh PATH so the current shell can see freshly installed winget packages.
$env:Path = (
    [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
    [System.Environment]::GetEnvironmentVariable("Path", "User")
)

if (-not (Get-Command tmux -ErrorAction SilentlyContinue)) {
    throw "tmux was not found on PATH. Open a new shell or reinstall tmux-windows."
}

# Reuse or create one repo-local leader session.
# tmux-windows is reliable when PowerShell sets cwd itself; using tmux -c is flaky here.
$launchCommand = "$powerShellExe -NoLogo -NoExit -Command ""Set-Location -LiteralPath '$repoRoot'"""
tmux new-session -A -s $SessionName $launchCommand
