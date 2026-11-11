# Explicit clipboard operation: legacy credential is a revocable API key, NOT a browser login password.
$ErrorActionPreference = 'Stop'
$keyFile = Join-Path $env:LOCALAPPDATA 'VM-Orchestrator\admin-key.txt'
if (-not (Test-Path -LiteralPath $keyFile -PathType Leaf)) {
    throw 'DPAPI key file not found. Start web console first.'
}
$secure = ConvertTo-SecureString -String (Get-Content -LiteralPath $keyFile -TotalCount 1).Trim()
$key = ([System.Net.NetworkCredential]::new('', $secure)).Password
if (-not $key) { throw 'DPAPI key is empty' }
Set-Clipboard -Value $key
$key = $null
Write-Host 'Legacy ADMIN API key copied. Browser login now uses username and password; revoke this key in Account Settings.' -ForegroundColor Yellow
Write-Host 'Clipboard can be read by other local applications while the key is present.' -ForegroundColor Yellow
Read-Host 'After using the key for API integration, press ENTER to clear clipboard'
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Clipboard]::Clear()
Write-Host 'Clipboard cleared.'
