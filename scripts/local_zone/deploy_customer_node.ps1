param(
    [string]$EdgeDomain = $env:EDGE_DOMAIN,
    [string]$ApiToken = $env:DIAGNOSTIC_API_TOKEN,
    [string]$CaddyExe = $env:CADDY_EXE,
    [switch]$NoGds2,
    [switch]$SkipHealthCheck
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent (Split-Path -Parent $scriptDir)
$cloudStartScript = Join-Path $projectDir "scripts\cloud_start_services.bat"
$edgeStartScript = Join-Path $scriptDir "start_edge_proxy.ps1"
$healthcheckScript = Join-Path $scriptDir "test_edge_health.ps1"

if ([string]::IsNullOrWhiteSpace($EdgeDomain)) {
    throw "EdgeDomain is required. Pass -EdgeDomain or set EDGE_DOMAIN."
}

$env:EDGE_DOMAIN = $EdgeDomain
$env:DIAGNOSTIC_API_HOST = "127.0.0.1"
$env:DIAGNOSTIC_API_PORT = "8080"
if (-not [string]::IsNullOrWhiteSpace($ApiToken)) {
    $env:DIAGNOSTIC_API_TOKEN = $ApiToken
}

Write-Host "Deploying customer node for domain: $EdgeDomain"
Write-Host "Project dir: $projectDir"

$cloudArgs = @()
if ($NoGds2) {
    $cloudArgs += "--no-gds2"
}

Write-Host "Starting cloud services..."
& $cloudStartScript @cloudArgs
if ($LASTEXITCODE -ne 0) {
    throw "cloud_start_services.bat failed with exit code $LASTEXITCODE"
}

Write-Host "Starting edge proxy..."
& $edgeStartScript -EdgeDomain $EdgeDomain -CaddyExe $CaddyExe -Background

Start-Sleep -Seconds 3

if ($SkipHealthCheck) {
    Write-Host "Skipping health checks."
    exit 0
}

Write-Host "Running health checks..."
& $healthcheckScript -EdgeHost $EdgeDomain -ApiToken $ApiToken
