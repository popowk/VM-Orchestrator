# Optional Windows interactive auto-logon setup for GUI apps, NOT required for the Guest Agent.
# Uses Microsoft's official Sysinternals Autologon interactive GUI.
# It NEVER collects/stores the Windows password in this script or command line.
param([string]$AutologonExe = '')
$ErrorActionPreference = 'Stop'
$admin = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Run as Administrator inside the customer Windows VM.'
}
if (-not $AutologonExe) {
  foreach ($name in @('Autologon64.exe','Autologon.exe')) {
    $test = Join-Path $PSScriptRoot $name
    if (Test-Path -LiteralPath $test -PathType Leaf) { $AutologonExe=$test; break }
  }
}
if (-not $AutologonExe -or -not (Test-Path -LiteralPath $AutologonExe -PathType Leaf)) {
  Write-Host 'Download Microsoft Sysinternals Autologon from:' -ForegroundColor Cyan
  $url='https://learn.microsoft.com/en-us/sysinternals/downloads/autologon'
  Write-Host $url
  Start-Process $url
  Write-Host 'Extract Autologon64.exe into this folder, then rerun this script.'
  exit 0
}
$signature = Get-AuthenticodeSignature -FilePath $AutologonExe
if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or
    $signature.SignerCertificate.Subject -notmatch 'Microsoft (Corporation|Windows)') {
  throw 'Autologon executable must have a VALID Microsoft Authenticode signature.'
}
Write-Warning 'Auto-logon makes the Windows desktop accessible after each boot; use a dedicated least-privilege VM account.'
Write-Warning 'Anyone with admin access to the VM may recover the stored credential. Do not use the host/admin/domain password.'
Write-Host 'Fill in the user/domain/password in the official GUI and choose Enable.' -ForegroundColor Cyan
Start-Process -FilePath $AutologonExe -Wait
Write-Host 'If you clicked Enable, Windows will try to auto-logon that VM account after the next boot.'
