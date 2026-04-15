param(
    [string]$Python32 = "",
    [string]$Python64 = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Resolve-PythonPath {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("x86", "x64")]
        [string]$Architecture,

        [string]$ConfiguredPath = ""
    )

    function Test-PyInstallerInstalled {
        param([Parameter(Mandatory = $true)][string]$PythonPath)
        $stdoutPath = [System.IO.Path]::GetTempFileName()
        $stderrPath = [System.IO.Path]::GetTempFileName()
        try {
            $proc = Start-Process `
                -FilePath $PythonPath `
                -ArgumentList @("-m", "PyInstaller", "--version") `
                -NoNewWindow `
                -Wait `
                -PassThru `
                -RedirectStandardOutput $stdoutPath `
                -RedirectStandardError $stderrPath
            return ($proc.ExitCode -eq 0)
        }
        finally {
            Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
        }
    }

    if ($ConfiguredPath) {
        if (-not (Test-Path -LiteralPath $ConfiguredPath)) {
            throw "Configured Python path does not exist: $ConfiguredPath"
        }
        if (-not (Test-PyInstallerInstalled -PythonPath $ConfiguredPath)) {
            throw "PyInstaller is not installed for: $ConfiguredPath"
        }
        return (Resolve-Path -LiteralPath $ConfiguredPath).Path
    }

    $launcherOutput = & py -0p 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $launcherOutput) {
        throw "Unable to query Python installations from 'py -0p'. Pass -Python32 and -Python64 explicitly."
    }

    $candidates = foreach ($line in $launcherOutput) {
        if ($line -match '^\s*-V:(?<version>\d+\.\d+)(?<arch>-32)?\s+\*?\s*(?<path>.+python(?:w)?\.exe)\s*$') {
            [pscustomobject]@{
                Version = [Version]("$($matches.version).0")
                Architecture = if ($matches.arch) { "x86" } else { "x64" }
                Path = $matches.path.Trim()
            }
        }
    }

    $matchingCandidates = $candidates |
        Where-Object { $_.Architecture -eq $Architecture } |
        Sort-Object Version -Descending

    if (-not $matchingCandidates) {
        throw "No $Architecture Python installation was found. Pass -Python32/-Python64 explicitly."
    }

    foreach ($candidate in $matchingCandidates) {
        if (Test-PyInstallerInstalled -PythonPath $candidate.Path) {
            return $candidate.Path
        }
    }

    throw "No $Architecture Python installation with PyInstaller was found. Pass -Python32/-Python64 explicitly."
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonExe,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    Write-Host ">> $PythonExe $($Arguments -join ' ')"
    & $PythonExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE"
    }
}

function Remove-PathIfExists {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

$Python32 = Resolve-PythonPath -Architecture x86 -ConfiguredPath $Python32
$Python64 = Resolve-PythonPath -Architecture x64 -ConfiguredPath $Python64

Write-Host "Using x86 Python: $Python32"
Write-Host "Using x64 Python: $Python64"

$BuildRoot = Join-Path $RepoRoot "build"
$DistRoot = Join-Path $RepoRoot "dist"
$Worker32Dist = Join-Path $DistRoot "_worker_x86"
$Worker64Dist = Join-Path $DistRoot "_worker_x64"
$ClientBuild = Join-Path $BuildRoot "pyinstaller_client"
$Worker32Build = Join-Path $BuildRoot "pyinstaller_worker_x86"
$Worker64Build = Join-Path $BuildRoot "pyinstaller_worker_x64"
$FinalClientDir = Join-Path $DistRoot "VCI_Proxy_Client"
$WorkersDir = Join-Path $FinalClientDir "workers"

if ($Clean) {
    Remove-PathIfExists -Path $Worker32Dist
    Remove-PathIfExists -Path $Worker64Dist
    Remove-PathIfExists -Path $FinalClientDir
    Remove-PathIfExists -Path $ClientBuild
    Remove-PathIfExists -Path $Worker32Build
    Remove-PathIfExists -Path $Worker64Build
}

$env:VCI_PROXY_WORKER_NAME = "VCI_Proxy_J2534_Worker_x86"
Invoke-Python -PythonExe $Python32 -Arguments @(
    "-m", "PyInstaller",
    "--clean",
    "--noconfirm",
    "--distpath", $Worker32Dist,
    "--workpath", $Worker32Build,
    "pyinstaller_j2534_worker.spec"
)

$env:VCI_PROXY_WORKER_NAME = "VCI_Proxy_J2534_Worker_x64"
Invoke-Python -PythonExe $Python64 -Arguments @(
    "-m", "PyInstaller",
    "--clean",
    "--noconfirm",
    "--distpath", $Worker64Dist,
    "--workpath", $Worker64Build,
    "pyinstaller_j2534_worker.spec"
)

Remove-Item Env:VCI_PROXY_WORKER_NAME -ErrorAction SilentlyContinue

Invoke-Python -PythonExe $Python64 -Arguments @(
    "-m", "PyInstaller",
    "--clean",
    "--noconfirm",
    "--distpath", $DistRoot,
    "--workpath", $ClientBuild,
    "pyinstaller_client.spec"
)

$Worker32Exe = Join-Path $Worker32Dist "VCI_Proxy_J2534_Worker_x86.exe"
$Worker64Exe = Join-Path $Worker64Dist "VCI_Proxy_J2534_Worker_x64.exe"

if (-not (Test-Path -LiteralPath $Worker32Exe)) {
    throw "Missing built worker executable: $Worker32Exe"
}
if (-not (Test-Path -LiteralPath $Worker64Exe)) {
    throw "Missing built worker executable: $Worker64Exe"
}
if (-not (Test-Path -LiteralPath $FinalClientDir)) {
    throw "Missing built client directory: $FinalClientDir"
}

New-Item -ItemType Directory -Force -Path $WorkersDir | Out-Null
Copy-Item -LiteralPath $Worker32Exe -Destination (Join-Path $WorkersDir "VCI_Proxy_J2534_Worker_x86.exe") -Force
Copy-Item -LiteralPath $Worker64Exe -Destination (Join-Path $WorkersDir "VCI_Proxy_J2534_Worker_x64.exe") -Force

Write-Host ""
Write-Host "Build completed:"
Write-Host "  $FinalClientDir"
Write-Host "Workers:"
Write-Host "  $(Join-Path $WorkersDir 'VCI_Proxy_J2534_Worker_x86.exe')"
Write-Host "  $(Join-Path $WorkersDir 'VCI_Proxy_J2534_Worker_x64.exe')"
