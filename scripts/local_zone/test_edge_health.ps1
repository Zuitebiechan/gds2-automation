param(
    [string]$EdgeHost = $env:EDGE_DOMAIN,
    [string]$ApiToken = $env:DIAGNOSTIC_API_TOKEN,
    [string]$Brand = "gds2",
    [switch]$SkipPublic
)

$ErrorActionPreference = "Stop"

$body = @{ brand = $Brand } | ConvertTo-Json -Compress
$headers = @{
    "Content-Type" = "application/json"
}

if (-not [string]::IsNullOrWhiteSpace($ApiToken)) {
    $headers["X-API-Token"] = $ApiToken
}

$loopbackUrl = "http://127.0.0.1:8080/api/session/start"
Write-Host "Checking loopback API: $loopbackUrl"
$loopbackResponse = Invoke-WebRequest -UseBasicParsing -Uri $loopbackUrl -Method POST -Headers $headers -Body $body
Write-Host ("Loopback status: {0}" -f $loopbackResponse.StatusCode)
Write-Host $loopbackResponse.Content

if ($SkipPublic) {
    Write-Host "Skipping public edge check."
    exit 0
}

if ([string]::IsNullOrWhiteSpace($EdgeHost)) {
    throw "EdgeHost is required for the public edge check. Pass -EdgeHost or set EDGE_DOMAIN."
}

$publicUrl = "https://$EdgeHost/api/session/start"
Write-Host "Checking public edge API: $publicUrl"
$publicResponse = Invoke-WebRequest -UseBasicParsing -Uri $publicUrl -Method POST -Headers $headers -Body $body
Write-Host ("Public edge status: {0}" -f $publicResponse.StatusCode)
Write-Host $publicResponse.Content
