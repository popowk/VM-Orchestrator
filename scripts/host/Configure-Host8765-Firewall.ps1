# Windows 10/11 Host: allow TCP 8765 ONLY from designated management LAN IPs.
# Example: .\Configure-Host8765-Firewall.ps1 -AllowedRemoteAddresses '192.168.2.0/24'
# This script does NOT remove other broad allow rules that may exist for Python.
param(
  [string[]]$AllowedRemoteAddresses = @('192.168.2.0/24'),
  [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$name = 'VMO Admin 8765 - Trusted Management LAN'
$admin = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Run PowerShell as Administrator on the VMware HOST.'
}
$existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
if ($Remove) {
  if ($existing) { $existing | Remove-NetFirewallRule }
  Write-Host 'The VMO trusted LAN firewall rule has been removed.'
  exit 0
}
if ($AllowedRemoteAddresses.Count -eq 0) { throw 'Specify one or more trusted management IPs/subnets.' }
foreach ($address in $AllowedRemoteAddresses) {
  if (-not $address -or $address -match '^(Any|\*|0\.0\.0\.0(?:/0)?|::/0|LocalSubnet)$') {
    throw "Unsafe broad remote address: $address. Use a specific trusted IP or CIDR."
  }
}
if ($existing) { $existing | Remove-NetFirewallRule }
New-NetFirewallRule -DisplayName $name -Direction Inbound -Enabled True `
  -Profile Private -Action Allow -Protocol TCP -LocalPort 8765 `
  -RemoteAddress $AllowedRemoteAddresses | Out-Null
Write-Host ('Allow TCP/8765 from: ' + ($AllowedRemoteAddresses -join ', ')) -ForegroundColor Green
Write-Warning 'Other existing broad allow rules (e.g. for python.exe/port 8765) can override this restriction. Audit them separately.'
Write-Warning 'LAN HTTP is unencrypted. Use a trusted HTTPS reverse proxy/VPN before entering admin credentials remotely.'
