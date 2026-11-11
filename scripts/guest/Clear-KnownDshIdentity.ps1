# Run INSIDE a verified *full clone*. No action unless -Apply and -IAmOnDisposableClone supplied.
# Only known DSH / fish-soul data locations are removed. Review all custom plugin configs manually.
[CmdletBinding(SupportsShouldProcess=$true, ConfirmImpact='High')]
param(
    [string]$DshHome = $(if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }),
    [switch]$Apply,
    [switch]$IAmOnDisposableClone
)
$ErrorActionPreference = 'Stop'
if (-not $Apply) {
    Write-Host 'DRY RUN. Nothing will be deleted. To actually clear, pass -Apply -IAmOnDisposableClone -Confirm:$false.'
}
if ($Apply -and -not $IAmOnDisposableClone) { throw 'Refusing: must explicitly confirm this is a disposable FULL CLONE.' }
if (-not (Test-Path -LiteralPath $DshHome)) { throw "DSH home does not exist: $DshHome" }
$targets = @(
    (Join-Path $DshHome '.credentials.yaml'),
    (Join-Path $DshHome '.env'),
    (Join-Path $DshHome 'fish-soul-config.json'),
    (Join-Path $DshHome 'fish-soul'),
    (Join-Path $DshHome 'sessions')
)
$profiles = Join-Path $DshHome 'profiles'
if (Test-Path -LiteralPath $profiles) {
    Get-ChildItem -LiteralPath $profiles -Directory | ForEach-Object {
        $targets += (Join-Path $_.FullName '.env')
    }
}
foreach ($target in $targets) {
    if (Test-Path -LiteralPath $target) {
        if ($Apply) {
            if ($PSCmdlet.ShouldProcess($target, 'DELETE identity/runtime data')) {
                Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop
                Write-Host "Cleared: $target"
            }
        } else {
            Write-Host "Would clear: $target"
        }
    }
}
Write-Warning 'NOT CLEARED: settings.yaml, home/profile cordis.patch.yml, Windows/browser/QQ credentials, SnowLuma data, other plugins, environment/registry, and free-space residue.'
Write-Warning 'Do not seal until you review those locations and the independent SnowLuma data directory.'
