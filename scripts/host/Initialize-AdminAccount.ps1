$ErrorActionPreference='Stop'
$project=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
. (Join-Path $PSScriptRoot 'Resolve-HostPython.ps1')
$python=Get-VMOHostPython -Project $project
$secretPath=Join-Path $env:LOCALAPPDATA 'VM-Orchestrator\admin-key.txt'
if (-not (Test-Path -LiteralPath $secretPath)) { throw 'Missing original DPAPI encryption key: start VMO once first.' }
$secure=ConvertTo-SecureString -String ((Get-Content -LiteralPath $secretPath -TotalCount 1).Trim())
$env:VMO_ADMIN_KEY=([System.Net.NetworkCredential]::new('',$secure)).Password
$bizPath=Join-Path $env:LOCALAPPDATA 'VM-Orchestrator\business-key.txt'
if (Test-Path -LiteralPath $bizPath) {
  $bs=ConvertTo-SecureString -String ((Get-Content -LiteralPath $bizPath -TotalCount 1).Trim())
  $env:VMO_BUSINESS_KEY=([System.Net.NetworkCredential]::new('',$bs)).Password
}
if (-not $env:VMO_DB_PATH) { $env:VMO_DB_PATH='D:\VMware\VM-Orchestrator\data\templates.sqlite3' }
Set-Location -LiteralPath $project
& $python -m app.admin_auth
if ($LASTEXITCODE -ne 0) { throw 'Administrator initialization failed' }
