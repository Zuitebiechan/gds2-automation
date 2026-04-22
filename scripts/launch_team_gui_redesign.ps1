param(
    [string]$WorkerSpec = "4:executor"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$omxCmd = Join-Path $env:APPDATA "npm\omx.cmd"
$env:Path = (
    [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
    [System.Environment]::GetEnvironmentVariable("Path", "User")
)

Set-Location -LiteralPath $repoRoot
$env:OMX_TEAM_WORKER_CLI = "codex"

$task = "Implement the approved client GUI redesign plan from .omx/plans/prd-client-gui-redesign.md and .omx/plans/test-spec-client-gui-redesign.md; split GUI delivery, backend vehicle-goal support, tests, and verification lanes."

Write-Host "[leader] cwd=$repoRoot"
Write-Host "[leader] worker_spec=$WorkerSpec"
Write-Host "[leader] task=$task"

if (-not (Test-Path -LiteralPath $omxCmd)) {
    throw "omx.cmd was not found at $omxCmd"
}

& $omxCmd team $WorkerSpec $task

Write-Host "[leader] omx team exited with code $LASTEXITCODE"
