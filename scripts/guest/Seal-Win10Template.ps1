# Run only on an unsealed disposable Windows BUILD VM. Sysprep then powers it off.
# No DSH/fish-soul application-data audit: the operator reviews credentials manually.
[CmdletBinding()]
param(
    [switch]$IAmOnDisposableClone,
    [switch]$IReviewedAllAppAndWindowsCredentials,
    [switch]$IUnderstandTheVmWillShutDown,
    [switch]$OmitVmMode,
    [switch]$UnattendedBootstrap
)
$ErrorActionPreference='Stop'
if (-not ($IAmOnDisposableClone -and $IReviewedAllAppAndWindowsCredentials -and
          $IUnderstandTheVmWillShutDown)) {
    throw 'Three explicit confirmations required: disposable BUILD, credentials manually reviewed, Sysprep shutdown understood.'
}
$admin=([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw 'Administrator privileges required inside the BUILD VM.' }
$sysprep=Join-Path $env:WINDIR 'System32\Sysprep\Sysprep.exe'
if (-not [Environment]::Is64BitProcess -and [Environment]::Is64BitOperatingSystem) {
    $sysprep=Join-Path $env:WINDIR 'Sysnative\Sysprep\Sysprep.exe'
}
if (-not (Test-Path -LiteralPath $sysprep -PathType Leaf)) {
    throw "Sysprep.exe not found: $sysprep"
}
$args=@('/generalize','/oobe','/shutdown')
if (-not $OmitVmMode) { $args += '/mode:vm' }
if ($UnattendedBootstrap) {
    $root=Join-Path $env:ProgramData 'VMO-OOBE'
    $generic=Join-Path $root 'Base-Unattend.xml'
    $stage=Join-Path $root 'Apply-PerCloneOOBE.ps1'
    $first=Join-Path $root 'FirstLogon-OOBE.ps1'
    $setup=Join-Path $env:WINDIR 'Setup\Scripts\SetupComplete.cmd'
    foreach ($required in @($generic,$stage,$first,$setup)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Missing OOBE bootstrap: $required. Run Prepare-TemplateGuest.ps1 first."
        }
    }
    [xml]$unattend=Get-Content -LiteralPath $generic -Raw -Encoding UTF8
    $passes=@($unattend.SelectNodes("//*[local-name()='settings']") |
        ForEach-Object { $_.GetAttribute('pass') })
    if ('specialize' -notin $passes -or 'oobeSystem' -notin $passes) {
        throw 'OOBE answer is missing specialize or oobeSystem pass.'
    }
    $state=Join-Path $env:ProgramFiles 'VM-Orchestrator\GuestAgent\state'
    foreach ($name in @('agent.json','machine-binding.json')) {
        if (Test-Path -LiteralPath (Join-Path $state $name)) {
            throw "Golden template contains customer Guest Agent identity ($name). Refusing Sysprep."
        }
    }
    $svc=Get-CimInstance Win32_Service -Filter "Name='VMO-GuestAgentV2'" -ErrorAction Stop
    if (-not $svc -or $svc.State -ne 'Running' -or $svc.StartMode -ne 'Auto' -or
        $svc.StartName -notin @('LocalSystem','NT AUTHORITY\SYSTEM') -or
        $svc.PathName -notlike '*GuestAgent*agent_v2.py*') {
        throw 'Required identity-free VMO Guest Agent V2 SYSTEM service is not ready.'
    }
    $args += ('/unattend:' + $generic)
}
Write-Host 'Sysprep starting. Successful sealing will shut down the BUILD VM.' -ForegroundColor Yellow
Write-Host 'Never boot this sealed source again. New clones enroll independently on first boot.'
$p=Start-Process -FilePath $sysprep -ArgumentList $args -PassThru -Wait
if ($p.ExitCode -ne 0) {
    throw "Sysprep exit $($p.ExitCode). Inspect Windows\System32\Sysprep\Panther setupact.log and setuperr.log."
}
