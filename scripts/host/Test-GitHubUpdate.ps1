# Read-only syntax check for the detached VMO updater.
# Called with -File so administrators can inspect the exact process command line.
param(
    [Parameter(Mandatory=$true)][string]$UpdaterPath
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $UpdaterPath -PathType Leaf)) {
    throw 'VMO updater script not found'
}
$tokens = $null
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
    $UpdaterPath, [ref]$tokens, [ref]$parseErrors
) | Out-Null
if ($parseErrors.Count -gt 0) {
    foreach ($item in $parseErrors) {
        Write-Error ('Updater syntax error: ' + $item.Message)
    }
    exit 2
}
Write-Output 'VMO updater PowerShell parser: PASS'
exit 0
