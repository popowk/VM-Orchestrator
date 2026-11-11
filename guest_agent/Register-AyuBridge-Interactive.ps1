# Run INSIDE the Guest using the Windows account that should see the Ayu Bridge UI.
# Registers a current-user, limited-privilege, interactive logon task.
# Guest Agent SYSTEM can later request: schtasks /run /tn "\VMO-AyuBridge-Interactive"
param([string]$ScriptDir='C:\dsh\Ayu-Breeze-Bridge-v1.4')
$ErrorActionPreference='Stop'
$script=Join-Path $ScriptDir 'Start.bat'
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw 'Ayu Bridge Start.bat was not found.' }
$account=[Security.Principal.WindowsIdentity]::GetCurrent().Name
if(-not $account) {throw 'Cannot determine the currently signed-in Windows user.'}
$taskName='VMO-AyuBridge-Interactive'
$action=New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR 'System32\cmd.exe') -Argument '/D /C "call Start.bat"' -WorkingDirectory $ScriptDir
$principal=New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $account
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Description 'Run Ayu Breeze Bridge in the signed-in customer session, not Guest Agent SYSTEM Session 0' -Force | Out-Null
Write-Host ('Interactive task registered for: '+$account) -ForegroundColor Green
Write-Host ('Task: '+$taskName)
Write-Host 'Ayu runs only when this user is logged in. SYSTEM remote commands do not create a visible GUI.' -ForegroundColor Yellow
Write-Host ('To start now: schtasks /run /tn "\'+$taskName+'"') -ForegroundColor Cyan
