@echo off
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  echo Please run as Administrator.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-GuestAgentV2.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo ERROR: Guest V2 install failed; check service and old task.
pause
exit /b %RC%
