@echo off
setlocal
set "VMO_TARGET=D:\VMware\VM-Orchestrator"
echo VMO recovery: %VMO_TARGET%
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Repair-Stuck-Update.ps1" -Target "%VMO_TARGET%" -ZipPath "%~dp0VM-Orchestrator-v1.4.43.zip"
if errorlevel 1 (
  echo Update did not complete. Check data\updates\updater.log
  pause
  exit /b 1
)
echo Upgrade complete.
pause
