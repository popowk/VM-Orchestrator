# VMO host-side GOLDEN IMAGE toolkit exporter. Never overwrites old kits or VMs.
[CmdletBinding()]
param([string]$DestinationRoot = 'D:\VMware\Template-Build')
$ErrorActionPreference='Stop'
$sourceRoot=Split-Path -Parent $PSScriptRoot
$version=(Get-Content -LiteralPath (Join-Path $sourceRoot 'VERSION') -Raw -Encoding UTF8).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') {throw 'VMO version invalid'}
$guestSource=Join-Path $sourceRoot 'scripts\guest'
$agentSource=Join-Path $sourceRoot 'guest_agent'
if (-not (Test-Path -LiteralPath (Join-Path $guestSource 'Install-OOBE-Bootstrap.ps1')) -or
    -not (Test-Path -LiteralPath (Join-Path $guestSource 'Seal-Win10Template.ps1')) -or
    -not (Test-Path -LiteralPath (Join-Path $agentSource 'Install-GuestAgentV2.ps1'))) {
    throw 'Guest template tools or Agent V2 installer are missing'
}
New-Item -ItemType Directory -Path $DestinationRoot -Force | Out-Null
$dest=Join-Path $DestinationRoot ('_GuestToolkit-v'+$version)
if (Test-Path -LiteralPath $dest) {
    throw "This toolkit already exists; do not overwrite: $dest"
}
New-Item -ItemType Directory -Path $dest -ErrorAction Stop | Out-Null
$guest=Join-Path $dest 'guest'
$agent=Join-Path $dest 'guest_agent'
New-Item -ItemType Directory -Path $guest,$agent -ErrorAction Stop | Out-Null
# Only release-shipped tools: never runtime app data, .dsh profiles,
# enrolled customer secrets, browser sessions or account databases.
Get-ChildItem -LiteralPath $guestSource -File | Where-Object {
    $_.Extension -in @('.ps1','.cmd','.bat','.xml','.md')
} | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $guest -ErrorAction Stop }
Get-ChildItem -LiteralPath $agentSource -File | Where-Object {
    $_.Extension -in @('.py','.ps1','.cmd','.json','.txt')
    -and $_.Name -notmatch '(?i)^(config\.json|credentials\.json|.*\.secret)$'
} | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $agent -ErrorAction Stop }
$note=@'
Independent VMO Golden Template Guest Kit
1. Copy this toolkit to the disposable Windows BUILD VM only.
2. In BUILD VM, run guest\Install-OOBE-Bootstrap.ps1 -ConfirmBuildClone as Administrator.
3. Configure Windows and inspect all account, application and browser secrets.
4. Run guest\Seal-Win10Template.ps1 with all required acknowledgement switches
   and -UnattendedBootstrap. Windows Sysprep must shut it down.
5. Do not start the sealed BUILD VM again. Remove installation ISO mount.
6. In Host management console, publish it as a fresh official template CANDIDATE.
7. Manually attest sealing in Host before assigning to customer VMs.
New customer applications are sent AFTER a new Guest Agent enrolls, not
preinstalled into a golden template with production customer credentials.
'@
[IO.File]::WriteAllText((Join-Path $dest 'BUILD-INSTRUCTIONS.txt'),$note,[Text.Encoding]::UTF8)
Write-Host ("Independent toolkit exported: "+$dest) -ForegroundColor Green
Write-Host "Existing official templates and customer VMs remain untouched."
