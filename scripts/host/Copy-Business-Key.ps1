# Windows Host only. Run with same user as Start-VM-Orchestrator.cmd.
$ErrorActionPreference = 'Stop'
$path = Join-Path $env:LOCALAPPDATA 'VM-Orchestrator\business-key.txt'
if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw 'Business key not initialized. Run Start-VM-Orchestrator.cmd once first.'
}
$secure = ConvertTo-SecureString ((Get-Content -LiteralPath $path -TotalCount 1).Trim())
$key = ([System.Net.NetworkCredential]::new('', $secure)).Password
if ($key.Length -lt 32) { throw 'Invalid business key length' }
Set-Clipboard -Value $key
Write-Host 'Business key copied. Paste into trusted SibHosting SERVER settings only.' -ForegroundColor Yellow
Write-Host 'Press ENTER to clear clipboard.'
[void][Console]::ReadLine()
try {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.Clipboard]::Clear()
    Write-Host 'Clipboard cleared.'
} catch {
    Write-Warning 'Could not clear clipboard automatically. Use Win+V to clear it.'
}
