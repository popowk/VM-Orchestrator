@echo off
setlocal
cd /d "%~dp0"
fltmc >nul 2>&1
if errorlevel 1 (
  echo ERROR: Run as administrator.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Configure-SnowLuma.ps1"
if errorlevel 1 (
  echo ERROR: SnowLuma adapter configuration failed.
  pause
  exit /b 1
)
echo SnowLuma adapter configured. Guest must send a fresh heartbeat before updates.
pause
