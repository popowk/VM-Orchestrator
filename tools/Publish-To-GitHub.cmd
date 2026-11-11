@echo off
setlocal
cd /d "%~dp0.."
where git.exe >nul 2>nul || (echo [ERROR] Install Git for Windows & pause & exit /b 1)
where gh.exe >nul 2>nul || (echo [ERROR] Install GitHub CLI ^& pause ^& exit /b 1)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Publish-To-GitHub.ps1" -PublicApproved
set RC=%ERRORLEVEL%
if not "%RC%"=="0" echo [ERROR] Publish failed. No release is assumed successful.
if "%RC%"=="0" echo Published source and pushed release tag. Check GitHub Actions for artifact completion.
pause
exit /b %RC%
