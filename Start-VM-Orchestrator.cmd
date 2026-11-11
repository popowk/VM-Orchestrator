@echo off
setlocal
cd /d "%~dp0"
title VM Orchestrator - Unified Backend
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\host\Start-Unified.ps1" %*
if errorlevel 1 (
  echo.
  echo VMO launch failed. Read the error above before retrying.
  pause
  exit /b 1
)
