# Optional one-time config. Default 192.168.2.99:8766 requires no action.
param([string]$ListenIp='192.168.2.99', [string]$GuestSubnet='192.168.50.0/24')
$ErrorActionPreference='Stop'
$project=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
. (Join-Path $PSScriptRoot 'Guest-Network.ps1')
# Validate before writing. This helper does NOT change Windows Firewall or iKuai rules.
if ($ListenIp -eq '0.0.0.0' -or $ListenIp -like '127.*') { throw 'Never use wildcard or loopback for Guest Gateway' }
Assert-GuestListenAddress $ListenIp
$path=Join-Path $project 'data\guest-network.json'
New-Item -ItemType Directory (Split-Path $path -Parent) -Force | Out-Null
$object=[ordered]@{ListenIp=$ListenIp;GuestSubnet=$GuestSubnet;Port=8766}
$object | ConvertTo-Json | Set-Content -LiteralPath $path -Encoding UTF8
$cfg=Get-GuestNetwork $project
Write-Host "Saved Gateway settings: $($cfg.ListenIp):$($cfg.Port), allowed guest subnet for firewall: $($cfg.GuestSubnet)" -ForegroundColor Green
Write-Warning 'Stop both services before changing the address. Registered Guest TLS CA migration may be necessary.'
