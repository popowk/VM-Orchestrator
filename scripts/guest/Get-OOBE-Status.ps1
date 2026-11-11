# Read-only OOBE diagnostics. Does not display passwords or API tokens.
$ErrorActionPreference='Continue'
$root=Join-Path $env:ProgramData 'VMO-OOBE'
Write-Host '== VM Orchestrator OOBE diagnostic (no secrets) ==' -ForegroundColor Cyan
Write-Host "Computer: $env:COMPUTERNAME"
Write-Host "OOBE root exists: $(Test-Path -LiteralPath $root)"
foreach($file in @('Base-Unattend.xml','Apply-PerCloneOOBE.ps1','stage.json','oobe-marker.json','bootstrap.log')){
 $path=Join-Path $root $file
 Write-Host "$file : $(Test-Path -LiteralPath $path)"
}
$reg=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State' -Name ImageState -ErrorAction SilentlyContinue
Write-Host "Windows ImageState: $($reg.ImageState)"
$drives=Get-CimInstance Win32_LogicalDisk -Filter 'DriveType = 5' -ErrorAction SilentlyContinue
Write-Host "Init ISO detected: $([bool]@($drives | Where-Object VolumeName -eq 'VMO_INIT').Count)"
$log=Join-Path $root 'bootstrap.log'
if(Test-Path -LiteralPath $log){Write-Host '== bootstrap.log last 25 lines (review for sensitive text) ==' ; Get-Content $log -Tail 25}
foreach($f in @("$env:WINDIR\Panther\setuperr.log", "$env:WINDIR\System32\Sysprep\Panther\setuperr.log")){
 if(Test-Path -LiteralPath $f){Write-Host "== $f (last 12 lines) ==";Get-Content -LiteralPath $f -Tail 12}
}
Write-Host 'Never share full Panther/Unattend XML or screenshots containing passwords.'
