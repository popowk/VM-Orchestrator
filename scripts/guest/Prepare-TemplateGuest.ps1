# Run inside a disposable Windows BUILD VM after installing Windows/VMware Tools/Python.
# Stage identity-free Guest Agent V2 and per-clone OOBE hooks BEFORE Sysprep.
[CmdletBinding()]
param([switch]$ConfirmBuildClone)
$ErrorActionPreference='Stop'
if (-not $ConfirmBuildClone) {
    throw 'Use -ConfirmBuildClone only inside a disposable, unsealed Windows BUILD VM.'
}
$admin=([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw 'Administrator privileges required inside the BUILD VM.' }
$bootstrap=Join-Path $PSScriptRoot 'Install-OOBE-Bootstrap.ps1'
$agent=Join-Path (Split-Path -Parent $PSScriptRoot) 'guest_agent\Install-GuestAgentV2.ps1'
if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf) -or
    -not (Test-Path -LiteralPath $agent -PathType Leaf)) {
    throw 'The complete exported Guest toolkit (guest and guest_agent folders) is required.'
}
$state=Join-Path $env:ProgramFiles 'VM-Orchestrator\GuestAgent\state'
foreach ($name in @('agent.json','machine-binding.json')) {
    if (Test-Path -LiteralPath (Join-Path $state $name)) {
        throw "Already enrolled/bound Guest Agent state found ($name). Use a clean BUILD clone."
    }
}
& $bootstrap -ConfirmBuildClone
if (-not $?) { throw 'OOBE bootstrap staging failed.' }
& $agent -TemplateMode
if (-not $?) { throw 'Identity-free Guest Agent V2 installation failed.' }
$svc=Get-CimInstance Win32_Service -Filter "Name='VMO-GuestAgentV2'" -ErrorAction Stop
if (-not $svc -or $svc.State -ne 'Running' -or $svc.StartMode -ne 'Auto' -or
    $svc.StartName -notin @('LocalSystem','NT AUTHORITY\SYSTEM')) {
    throw 'Guest Agent V2 must be running automatically as SYSTEM before Sysprep.'
}
foreach ($name in @('agent.json','machine-binding.json')) {
    if (Test-Path -LiteralPath (Join-Path $state $name)) {
        throw "Guest Agent acquired identity during template preparation ($name). Refusing to proceed."
    }
}
Write-Host 'PASS: OOBE hooks and identity-free Guest Agent V2 are prepared.' -ForegroundColor Green
Write-Host 'The running Agent is waiting for a unique per-customer VMO_INIT ISO on first boot.'
Write-Host 'After manual review run Seal-Win10Template.ps1 -UnattendedBootstrap with all required confirmations.'
