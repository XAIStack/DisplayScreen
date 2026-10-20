#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Install displayshow as a Windows service (DisplayScreen).

.USAGE
  Right-click PowerShell -> Run as administrator, then:
    cd <path>\windows-screen-service
    .\install.ps1

  Optional:
    .\install.ps1 -InstallDir "C:\Program Files\displayshow"
    .\install.ps1 -ForceConfig
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "$env:ProgramData\displayshow",
    [switch]$ForceConfig
)

$ErrorActionPreference = "Stop"
$SourceDir = $PSScriptRoot

function Write-Step([string]$Message) {
    Write-Host "[install] $Message"
}

function Assert-Python {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) {
        $py = Get-Command py -ErrorAction SilentlyContinue
    }
    if (-not $py) {
        throw "Python 3.9+ not found. Install from https://www.python.org/ and enable 'Add to PATH'."
    }
    $verText = & $py.Source -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    $parts = $verText.Trim().Split(".")
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 9)) {
        throw "Need Python >= 3.9 (found $verText)"
    }
    return $py.Source
}

Write-Step "source = $SourceDir"
Write-Step "target = $InstallDir"

$python = Assert-Python
Write-Step "python = $python ($(& $python -c 'import sys; print(sys.version.split()[0])'))"

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$files = @(
    "screen_updater.py",
    "system_metrics.py",
    "levetop_protocol.py",
    "win_service.py",
    "requirements.txt",
    "VERSION",
    "run_screen_updater.bat",
    "install.ps1",
    "uninstall.ps1",
    "部署手册.md",
    "DEPLOYMENT.en.md",
    "README.md"
)

foreach ($name in $files) {
    $from = Join-Path $SourceDir $name
    if (Test-Path $from) {
        Copy-Item -Force $from (Join-Path $InstallDir $name)
    }
}

$configSrc = Join-Path $SourceDir "config.json"
$configDst = Join-Path $InstallDir "config.json"
if ((Test-Path $configDst) -and -not $ForceConfig) {
    Write-Step "keeping existing config.json (use -ForceConfig to replace)"
} elseif (Test-Path $configSrc) {
    Copy-Item -Force $configSrc $configDst
}

$venv = Join-Path $InstallDir ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Step "creating venv at $venv"
    & $python -m venv $venv
}

Write-Step "installing Python dependencies"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $InstallDir "requirements.txt")
& $venvPython -m pywin32_postinstall -install

# Stop existing service if present
$svc = Get-Service -Name "DisplayScreen" -ErrorAction SilentlyContinue
if ($svc) {
    Write-Step "stopping existing DisplayScreen service"
    try { Stop-Service DisplayScreen -Force -ErrorAction SilentlyContinue } catch {}
    & $venvPython (Join-Path $InstallDir "win_service.py") stop 2>$null
    Start-Sleep -Seconds 2
}

Push-Location $InstallDir
try {
    Write-Step "registering Windows service DisplayScreen"
    & $venvPython .\win_service.py --startup auto install
    # Point service binary at venv python hosting this script
    $binPath = "`"$venvPython`" `"$(Join-Path $InstallDir 'win_service.py')`""
    sc.exe config DisplayScreen binPath= $binPath start= auto | Out-Null
    sc.exe description DisplayScreen "LEVETOP serial front-panel display (displayshow)" | Out-Null

    Write-Step "starting DisplayScreen"
    Start-Service DisplayScreen
} finally {
    Pop-Location
}

Start-Sleep -Seconds 2
Get-Service DisplayScreen | Format-List Name, Status, StartType
Write-Step "done. Logs: Event Viewer -> Windows Logs -> Application (source Python Service)"
Write-Step "manual dry-run: `"$venvPython`" -u `"$InstallDir\screen_updater.py`" --dry-run --once"
Write-Step "docs: $InstallDir\部署手册.md  |  $InstallDir\DEPLOYMENT.en.md"
