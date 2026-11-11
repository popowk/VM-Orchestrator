# One-time recovery for a stuck VMO Host self-update. Windows PowerShell 5.1.
# Run as the SAME logged-in Windows user that runs VMware/Host, not SYSTEM.
param(
    [string]$Target = 'D:\VMware\VM-Orchestrator',
    [string]$ZipPath = (Join-Path $PSScriptRoot 'VM-Orchestrator-v1.4.42.zip')
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
    $stateDir=Join-Path $Target 'data\updates'
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip=[IO.Compression.ZipFile]::OpenRead($ZipPath)
    $version=$null
    $runner=Join-Path $stateDir 'Repair-Updater.ps1'
    try {
        $verEntry=$zip.GetEntry('VM-Orchestrator-v1.4.42/VERSION')
        $scriptEntry=$zip.GetEntry('VM-Orchestrator-v1.4.42/scripts/host/Apply-GitHubUpdate.ps1')
        if (-not $verEntry -or -not $scriptEntry) { throw 'Missing VERSION or detached updater in ZIP' }
        $sr=New-Object IO.StreamReader($verEntry.Open())
        try { $version=$sr.ReadToEnd().Trim() } finally { $sr.Dispose() }
        if ($version -ne '1.4.42') { throw "Unsupported recovery archive version: $version" }
        $sourceStream=$scriptEntry.Open()
        $targetStream=[IO.File]::Open($runner,[IO.FileMode]::Create,[IO.FileAccess]::Write)
        try { $sourceStream.CopyTo($targetStream) } finally { $targetStream.Dispose();$sourceStream.Dispose() }
        # Normalize the updater to UTF-8 BOM for Windows PowerShell 5.1.
        $runnerCode = [IO.File]::ReadAllText($runner, [Text.Encoding]::UTF8)
        [IO.File]::WriteAllText($runner, $runnerCode, (New-Object System.Text.UTF8Encoding($true)))
    } finally { $zip.Dispose() }
    $expected=(Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $shaPath=$ZipPath+'.sha256'
    if (Test-Path -LiteralPath $shaPath -PathType Leaf) {
        $line=(Get-Content -LiteralPath $shaPath -Raw).Trim()
        if ($line -notmatch '^([a-fA-F0-9]{64})\s+\*?VM-Orchestrator-v1\.4\.42\.zip$') {
            throw 'Invalid SHA256 manifest format'
        }
        if ($Matches[1].ToLowerInvariant() -ne $expected) { throw 'ZIP SHA256 mismatch' }
    } else {
        Write-Warning 'SHA256 sidecar not found. Download the matching checksum from the GitHub Release.'
    }
    $hostPid=0
    $listener=Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $p=Get-CimInstance Win32_Process -Filter ("ProcessId="+$listener.OwningProcess) -ErrorAction Stop
        if (-not $p -or $p.Name -notmatch '^python(w)?\.exe$' -or
            $p.CommandLine -notmatch 'app\.unified_server') {
            throw 'Port 8765 is owned by an unknown process. Refusing to replace it.'
        }
        $hostPid=[int]$p.ProcessId
    }
    Write-Host "VMO recovery target: $Target" -ForegroundColor Cyan
    Write-Host "Updating to $version; existing Host PID: $hostPid" -ForegroundColor Cyan
    Write-Host 'The existing code is backed up before replacement. Customer data is preserved.' -ForegroundColor Cyan
    & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner `
        -ZipPath $ZipPath -ExpectedSha256 $expected -Target $Target -HostPid $hostPid -Version $version
    if ($LASTEXITCODE -ne 0) { throw 'Detached updater failed. Inspect data\updates\updater.log' }
    Write-Host 'Host started and passed the health check.' -ForegroundColor Green
} catch {
    Write-Host ('Recovery failed: '+$_.Exception.Message) -ForegroundColor Red
    exit 1
}
