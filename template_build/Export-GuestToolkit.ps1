# In-project one-click entry; compatible with Host upgrades from v1.4.62.
[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$exporter=Join-Path $projectRoot 'scripts\host\Export-GuestToolkit.ps1'
if (-not (Test-Path -LiteralPath $exporter -PathType Leaf)) {
    throw ('Host exporter missing: '+$exporter)
}
& $exporter
