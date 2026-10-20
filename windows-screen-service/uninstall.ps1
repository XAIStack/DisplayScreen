#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Stop and remove the DisplayScreen Windows service.
  Files under -InstallDir are kept unless -RemoveFiles is set.

.USAGE
  .\uninstall.ps1
  .\uninstall.ps1 -RemoveFiles
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "$env:ProgramData\displayshow",
    [switch]$RemoveFiles
)

$ErrorActionPreference = "Continue"

Write-Host "[uninstall] stopping DisplayScreen"
$svc = Get-Service -Name "DisplayScreen" -ErrorAction SilentlyContinue
if ($svc) {
    try { Stop-Service DisplayScreen -Force -ErrorAction SilentlyContinue } catch {}
}

$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
$winService = Join-Path $InstallDir "win_service.py"
if ((Test-Path $venvPython) -and (Test-Path $winService)) {
    & $venvPython $winService stop 2>$null
    & $venvPython $winService remove 2>$null
} else {
    sc.exe stop DisplayScreen | Out-Null
    sc.exe delete DisplayScreen | Out-Null
}

Start-Sleep -Seconds 1
$left = Get-Service -Name "DisplayScreen" -ErrorAction SilentlyContinue
if ($left) {
    sc.exe delete DisplayScreen | Out-Null
}

if ($RemoveFiles) {
    Write-Host "[uninstall] removing $InstallDir"
    Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
} else {
    Write-Host "[uninstall] service removed; files kept at $InstallDir"
}

Write-Host "[uninstall] done"
