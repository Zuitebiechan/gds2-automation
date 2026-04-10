param(
    [string]$EdgeDomain = $env:EDGE_DOMAIN,
    [string]$CaddyExe = $env:CADDY_EXE,
    [string]$ConfigPath = "",
    [string]$LogDir = "",
    [switch]$Background
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent (Split-Path -Parent $scriptDir)

if ([string]::IsNullOrWhiteSpace($EdgeDomain)) {
    throw "EDGE_DOMAIN is required. Pass -EdgeDomain or set the EDGE_DOMAIN environment variable."
}

if ([string]::IsNullOrWhiteSpace($CaddyExe)) {
    $CaddyExe = "caddy"
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $scriptDir "Caddyfile.example"
}

if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $projectDir "logs"
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Caddy config file not found: $ConfigPath"
}

$env:EDGE_DOMAIN = $EdgeDomain
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$stdoutLog = Join-Path $LogDir "caddy-edge.log"
$stderrLog = Join-Path $LogDir "caddy-edge-error.log"
$args = @("run", "--config", $ConfigPath, "--adapter", "caddyfile")

Write-Host "Starting Local Zone edge proxy for domain: $EdgeDomain"
Write-Host "Caddy binary: $CaddyExe"
Write-Host "Config file:   $ConfigPath"
Write-Host "Stdout log:    $stdoutLog"
Write-Host "Stderr log:    $stderrLog"

if ($Background) {
    Start-Process `
        -FilePath $CaddyExe `
        -ArgumentList $args `
        -WorkingDirectory $projectDir `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -WindowStyle Hidden
    Write-Host "Caddy started in the background."
    exit 0
}

& $CaddyExe @args
