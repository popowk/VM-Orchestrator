@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "VMO_PS=%~dp0scripts\host"
if /i "%~1"=="account" goto account
if /i "%~1"=="network" goto network
if /i "%~1"=="guestfw" goto guestfw
if /i "%~1"=="adminfw" goto adminfw
if /i "%~1"=="autostart" goto autostart
if /i "%~1"=="noautostart" goto noautostart
if /i "%~1"=="businesskey" goto businesskey
if /i "%~1"=="loginkey" goto loginkey
if /i "%~1"=="deps" goto deps
if /i "%~1"=="github" goto github
:menu
cls
echo ========================================================
echo     VM Orchestrator 1.4.37 - Host Management
 echo ========================================================
echo  1. Initialize administrator account
echo  2. Configure Guest gateway network
echo  3. Configure Guest gateway firewall [Admin]
echo  4. Restrict Host management port 8765 [Admin]
echo  5. Enable auto-start on login
echo  6. Disable auto-start on login
echo  7. Copy business API key [Sensitive]
echo  8. Copy login API key [Sensitive]
echo  9. Repair Python dependencies
echo 10. Configure GitHub release update source (public / private)
echo  0. Exit
 echo.
set "OPTION="
set /p "OPTION=Select operation: "
if "%OPTION%"=="1" goto account
if "%OPTION%"=="2" goto network
if "%OPTION%"=="3" goto guestfw
if "%OPTION%"=="4" goto adminfw
if "%OPTION%"=="5" goto autostart
if "%OPTION%"=="6" goto noautostart
if "%OPTION%"=="7" goto businesskey
if "%OPTION%"=="8" goto loginkey
if "%OPTION%"=="9" goto deps
if "%OPTION%"=="10" goto github
if "%OPTION%"=="0" exit /b 0
goto menu
:account
call :requireAdmin || goto failed
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VMO_PS%\Initialize-AdminAccount.ps1"
goto done
:network
set "IP=192.168.2.99"
set "SUBNET=192.168.50.0/24"
set "INPUT_IP="
echo Gateway host IP [default 192.168.2.99]:
set /p "INPUT_IP=> "
if defined INPUT_IP set "IP=%INPUT_IP%"
set "INPUT_SUBNET="
echo Guest subnet [default 192.168.50.0/24]:
set /p "INPUT_SUBNET=> "
if defined INPUT_SUBNET set "SUBNET=%INPUT_SUBNET%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VMO_PS%\Configure-GuestNetwork.ps1" -ListenIp "%IP%" -GuestSubnet "%SUBNET%"
goto done
:guestfw
call :requireAdmin || goto failed
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VMO_PS%\Configure-GuestFirewall.ps1"
goto done
:adminfw
call :requireAdmin || goto failed
set "INPUT_ADMIN_SUBNET="
echo Allowed administrator subnet [default 192.168.2.0/24]:
set "ADMIN_SUBNET=192.168.2.0/24"
set /p "INPUT_ADMIN_SUBNET=> "
if defined INPUT_ADMIN_SUBNET set "ADMIN_SUBNET=%INPUT_ADMIN_SUBNET%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VMO_PS%\Configure-Host8765-Firewall.ps1" -AllowedRemoteAddresses "%ADMIN_SUBNET%"
goto done
:autostart
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VMO_PS%\Set-LoginAutoStart.ps1" -Enable
goto done
:noautostart
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VMO_PS%\Set-LoginAutoStart.ps1" -Disable
goto done
:businesskey
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VMO_PS%\Copy-Business-Key.ps1"
goto done
:loginkey
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VMO_PS%\Copy-Login-Key.ps1"
goto done
:deps
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VMO_PS%\Install-HostPythonDeps.ps1"
goto done
:github
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VMO_PS%\Configure-GitHubUpdate.ps1"
goto done
:requireAdmin
fltmc >nul 2>&1
if errorlevel 1 (echo ERROR: Run Manage.cmd as Administrator. & exit /b 1)
exit /b 0
:failed
if "%~1"=="" pause
exit /b 1
:done
if errorlevel 1 (echo FAILED - read errors above. & pause & exit /b 1)
echo Completed successfully.
if "%~1"=="" (pause & goto menu)
exit /b 0
