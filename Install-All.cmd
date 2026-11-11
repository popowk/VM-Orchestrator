@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-All.ps1"
if errorlevel 1 ( echo. & echo Installation failed. & pause )
