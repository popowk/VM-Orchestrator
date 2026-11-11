# One-time, opt-in restricted Windows Firewall allow rule for Guest Gateway ONLY.
# This does not replace iKuai ACL or enforce same-subnet VM-to-VM isolation.
$ErrorActionPreference='Stop'
$project=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
. (Join-Path $PSScriptRoot 'Guest-Network.ps1')
$cfg=Get-GuestNetwork $project
Assert-GuestListenAddress $cfg.ListenIp
$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run Manage.cmd guestfw as Administrator.'
}
$name='VMO Guest Gateway - Restricted TCP 8766'
$old=Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
if ($old) { Remove-NetFirewallRule -DisplayName $name -ErrorAction Stop }
New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Enabled True -Profile Any `
    -Protocol TCP -LocalPort 8766 -LocalAddress $cfg.ListenIp -RemoteAddress $cfg.GuestSubnet | Out-Null
Write-Host "Allowed ONLY source $($cfg.GuestSubnet) -> host $($cfg.ListenIp):8766 TCP in Windows Firewall." -ForegroundColor Green
Write-Warning 'Other pre-existing broad inbound allow rules may still apply. Review with Get-NetFirewallRule and restrict access in iKuai as well.'
