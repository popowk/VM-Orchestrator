@echo off
setlocal
set "PYTHON="
for %%V in (314 313 312 311) do if exist "C:\Program Files\Python%%V\python.exe" if not defined PYTHON set "PYTHON=C:\Program Files\Python%%V\python.exe"
set "TOOL=C:\Program Files\VM-Orchestrator\GuestAgent\app_registry.py"
if not exist "%PYTHON%" (echo Python not found: %PYTHON% & pause & exit /b 1)
if not exist "%TOOL%" (echo Agent is not installed: %TOOL% & pause & exit /b 1)
set /p APP=Application (snowluma or dsh-fish-soul):
set /p URL=Local web URL (http://127.0.0.1:5099):
"%PYTHON%" "%TOOL%" register --app "%APP%" --url "%URL%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo Registration failed; check Guest Agent V2 local reporter (127.0.0.1:18767).
pause
exit /b %RC%
