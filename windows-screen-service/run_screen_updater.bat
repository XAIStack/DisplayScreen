@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PYTHON=%~dp0.venv\Scripts\python.exe"
) else if exist "%ProgramData%\displayshow\.venv\Scripts\python.exe" (
  set "PYTHON=%ProgramData%\displayshow\.venv\Scripts\python.exe"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo python not found. Run install.ps1 first.
    exit /b 1
  )
  set "PYTHON=python"
)

"%PYTHON%" -u "%~dp0screen_updater.py" %*
