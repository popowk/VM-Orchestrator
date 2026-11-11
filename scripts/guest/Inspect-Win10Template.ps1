# Run INSIDE the disposable Windows 10 template BUILD CLONE, in PowerShell 5.1+.
# Produces filenames and categories ONLY: never reads or prints credential contents.
param(
    [string]$DshHome = $(if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }),
    [string]$SnowLumaDataDir = ''
)
$ErrorActionPreference = 'Stop'
$findings = New-Object System.Collections.ArrayList
function Add-Finding([string]$Path, [string]$Severity, [string]$Reason) {
    if (Test-Path -LiteralPath $Path) {
        [void]$findings.Add([pscustomobject]@{ severity=$Severity; path=$Path; reason=$Reason })
    }
}
$blockers = @(
    @{suffix='.credentials.yaml'; reason='DSH managed secrets'},
    @{suffix='.env'; reason='DSH fallback API keys'},
    @{suffix='fish-soul-config.json'; reason='fish-soul settings may include API keys'},
    @{suffix='fish-soul'; reason='fish-soul memory, credentials, browser sessions and backups'},
    @{suffix='sessions'; reason='DSH chat/session logs'}
)
foreach ($entry in $blockers) {
    Add-Finding (Join-Path $DshHome $entry.suffix) 'BLOCK' $entry.reason
}
Add-Finding (Join-Path $DshHome 'settings.yaml') 'REVIEW' 'Review for user paths, secret references and custom providers'
Add-Finding (Join-Path $DshHome 'cordis.patch.yml') 'REVIEW' 'Home-level plugin settings may contain tokens'
$profiles = Join-Path $DshHome 'profiles'
if (Test-Path -LiteralPath $profiles) {
    Get-ChildItem -LiteralPath $profiles -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        Add-Finding (Join-Path $_.FullName 'cordis.patch.yml') 'REVIEW' 'Profile patch may contain tokens or account IDs'
        Add-Finding (Join-Path $_.FullName '.env') 'BLOCK' 'Profile environment secrets'
    }
}
if ($SnowLumaDataDir) {
    Add-Finding $SnowLumaDataDir 'REVIEW' 'SnowLuma data path; must be newly initialized and cleared of account/login state'
    Add-Finding (Join-Path $SnowLumaDataDir 'config\onebot.json') 'BLOCK' 'OneBot configuration may include access token'
    Add-Finding (Join-Path $SnowLumaDataDir 'config') 'REVIEW' 'SnowLuma per-account config and runtime settings'
} else {
    [void]$findings.Add([pscustomobject]@{severity='REVIEW';path='SnowLuma data location unknown';reason='Specify -SnowLumaDataDir after checking installation'})
}
[void]$findings.Add([pscustomobject]@{severity='REVIEW';path='Windows Credential Manager / browser profiles / scheduled tasks / environment / other user directories';reason='Manual inspection required; not automatically verified'})
[pscustomobject]@{
    machine = $env:COMPUTERNAME
    dsh_home = $DshHome
    dsh_runtime_present = (Test-Path -LiteralPath (Join-Path $DshHome 'dsh-runtimes'))
    dsh_profile_present = (Test-Path -LiteralPath $profiles)
    findings = @($findings)
    blocked_count = @($findings | Where-Object severity -eq 'BLOCK').Count
    note = 'This is a targeted metadata audit, NOT a proof of no remaining credentials.'
} | ConvertTo-Json -Depth 5
