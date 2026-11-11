# One-time recovery for a stuck VMO Host self-update. Windows PowerShell 5.1.
# Run as the SAME logged-in Windows user that runs VMware/Host, not SYSTEM.
param(
    [string]$Target = 'D:\VMware\VM-Orchestrator',
    [string]$ZipPath = (Join-Path $PSScriptRoot 'VM-Orchestrator-v1.4.48.zip')
)
$ErrorActionPreference='Stop'
try {
    $Target = [IO.Path]::GetFullPath($Target)
    $ZipPath = [IO.Path]::GetFullPath($ZipPath)
    if (-not (Test-Path -LiteralPath (Join-Path $Target 'app\main.py') -PathType Leaf)) {
        throw "VMO installation folder not found: $Target. Supply -Target with the correct path."
    }
    if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
        throw "Update archive not found: $ZipPath"
    }
    if ([IO.Path]::GetFileName($ZipPath) -notmatch '^VM-Orchestrator-v([0-9]+\.[0-9]+\.[0-9]+)\.zip$') {
        throw 'Expected VM-Orchestrator-vX.Y.Z.zip archive filename'
    }
    $packageVersion=$Matches[1]
    $stateDir=Join-Path $Target 'data\updates'
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip=[IO.Compression.ZipFile]::OpenRead($ZipPath)
    $version=$null
    $runner=Join-Path $stateDir 'Repair-Updater.ps1'
    try {
        $verEntry=$zip.GetEntry('VM-Orchestrator-v' + $packageVersion + '/VERSION')
        $scriptEntry=$zip.GetEntry('VM-Orchestrator-v' + $packageVersion + '/scripts/host/Apply-GitHubUpdate.ps1')
        if (-not $verEntry -or -not $scriptEntry) { throw 'Missing VERSION or detached updater in ZIP' }
        $sr=New-Object IO.StreamReader($verEntry.Open())
        try { $version=$sr.ReadToEnd().Trim() } finally { $sr.Dispose() }
        if ($version -ne $packageVersion) { throw "Unsupported recovery archive version: $version" }
        $sourceStream=$scriptEntry.Open()
        $targetStream=[IO.File]::Open($runner,[IO.FileMode]::Create,[IO.FileAccess]::Write)
        try { $sourceStream.CopyTo($targetStream) } finally { $targetStream.Dispose();$sourceStream.Dispose() }
        # PowerShell 5.1 does not reliably parse UTF-8-without-BOM files with non-ASCII literals.
        # Normalize the extracted updater regardless of the ZIP build environment.
        $runnerCode = [IO.File]::ReadAllText($runner, [Text.Encoding]::UTF8)
        [IO.File]::WriteAllText($runner, $runnerCode, (New-Object System.Text.UTF8Encoding($true)))
    } finally { $zip.Dispose() }
    $expected=(Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $shaPath=$ZipPath+'.sha256'
    if (Test-Path -LiteralPath $shaPath -PathType Leaf) {
        $line=(Get-Content -LiteralPath $shaPath -Raw).Trim()
        if ($line -notmatch '^([a-fA-F0-9]{64})\s+\*?VM-Orchestrator-v[0-9]+\.[0-9]+\.[0-9]+\.zip$') {
            throw 'Invalid SHA256 manifest format'
        }
        if ($Matches[1].ToLowerInvariant() -ne $expected -or $line -notmatch ('\s+\*?' + [regex]::Escape([IO.Path]::GetFileName($ZipPath)) + '$')) { throw 'ZIP SHA256 mismatch' }
    } else {
        Write-Warning 'SHA256 sidecar not found. Download the matching checksum from the GitHub Release.'
    }
    # The detached updater rechecks BOTH port owners and the authenticated
    # VMO health/version before terminating a running Host process. Passing
    # 0 is valid when no Host was running during recovery startup.
    $hostPid=0
    Write-Host "VMO recovery target: $Target" -ForegroundColor Cyan
    Write-Host "Updating to $version; Host ownership will be verified by detached updater" -ForegroundColor Cyan
    Write-Host 'The existing code is backed up before replacement. Customer data is preserved.' -ForegroundColor Cyan
    & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner `
        -ZipPath $ZipPath -ExpectedSha256 $expected -Target $Target -HostPid $hostPid -Version $version
    if ($LASTEXITCODE -ne 0) { throw 'Detached updater failed. Inspect data\updates\updater-*.log' }
    Write-Host 'Host started and passed the health check.' -ForegroundColor Green
} catch {
    Write-Host ('Recovery failed: '+$_.Exception.Message) -ForegroundColor Red
    exit 1
}
