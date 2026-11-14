@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Export-GuestToolkit.ps1"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo [ERROR] Guest toolkit export failed. Exit code: %RESULT%
) else (
    echo [OK] Guest toolkit exported into template_build.
)
pause
exit /b %RESULT%
