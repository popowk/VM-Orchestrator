# VM Orchestrator V1.4.39 system Python + remote task recovery (Windows PowerShell 5.1 compatible)
# Keep database, TLS, DPAPI keys, VMs and templates. Ignore legacy .venv.
param(
    [string]$Target = 'D:\VMware\VM-Orchestrator',
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
$source = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$Target = [IO.Path]::GetFullPath($Target).TrimEnd('\')
if ($source.Equals($Target, [StringComparison]::OrdinalIgnoreCase) -or
    $source.StartsWith($Target + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $Target.StartsWith($source + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Extract the ZIP into a separate directory, not inside the installed project.'
}

# Refuse to upgrade if the Host Agent port is in use. A running agent may write the DB.
$client = New-Object System.Net.Sockets.TcpClient
$portBusy = $false
try {
    $pending = $client.BeginConnect('127.0.0.1', 8765, $null, $null)
    if ($pending.AsyncWaitHandle.WaitOne(800)) {
        try {
            $client.EndConnect($pending)
            $portBusy = $true
        } catch [System.Net.Sockets.SocketException] {
            $portBusy = $false
        }
    }
} finally {
    $client.Close()
}
# The Guest HTTPS service is a second SQLite writer. Reject upgrade if its listener is active.
$guestPortBusy = @((Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue)).Count -gt 0
if ($guestPortBusy) { throw 'Port 8766 is busy. Stop the Guest Gateway before upgrading.' }
if ($portBusy) {
    throw 'Port 8765 is busy. Stop the existing Host Agent with Ctrl+C before upgrading.'
}

# Verify the installed Python before touching the existing installation or data.
. (Join-Path $source 'scripts\host\Resolve-HostPython.ps1')
$systemPython = Get-VMOHostPython -Project $Target
Write-Host "System Python found: $systemPython" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $Target -PathType Container)) {
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$releaseVersion = (Get-Content -LiteralPath (Join-Path $source 'VERSION') -Raw).Trim()
$backup = $null
if (Test-Path -LiteralPath (Join-Path $Target 'app\main.py') -PathType Leaf) {
    $backup = Join-Path $Target ('backup-before-v' + $releaseVersion + '-' + $stamp)
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    $currentSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $selfGrant='*' + $currentSid + ':(OI)(CI)F'
    $null=& icacls.exe $backup /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' $selfGrant 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Cannot secure backup directory holding database and TLS secrets.' }
    foreach ($folder in @('app', 'scripts', 'data')) {
        $original = Join-Path $Target $folder
        if (Test-Path -LiteralPath $original) {
            Copy-Item -LiteralPath $original -Destination $backup -Recurse -Force
        }
    }
    # template_build is a LIVE workspace. Back up script files, never VM disks.
    $templateTools=@('README.md','Export-GuestToolkit.ps1')
    foreach ($tool in $templateTools) {
        $srcTool=Join-Path (Join-Path $Target 'template_build') $tool
        if (Test-Path -LiteralPath $srcTool -PathType Leaf) {
            $toolBackup=Join-Path $backup 'template_build'
            New-Item -ItemType Directory -Path $toolBackup -Force | Out-Null
            Copy-Item -LiteralPath $srcTool -Destination (Join-Path $toolBackup $tool) -Force
        }
    }
    Write-Host "Backup created: $backup" -ForegroundColor Cyan
}

# Copy relative files, never source data/.venv, and never overwrite user .env.
foreach ($required in @('app\main.py', 'scripts\host\Start-Unified.ps1', 'Start-VM-Orchestrator.cmd')) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $required) -PathType Leaf)) {
        throw "Missing package file: $required"
    }
}
$copied = 0
Get-ChildItem -LiteralPath $source -Recurse -File -Force | ForEach-Object {
    $relative = $_.FullName.Substring($source.Length).TrimStart('\')
    if ($relative -match '^(data|dist|\.venv|\.git|backup-before-)[\\/]') { return }
    if ($relative -eq '.env') { return }
    # Whitelist ONLY source tools inside template_build; it contains live BUILD
    # VMDKs, exported Guest toolkits and possibly customer setup material.
    if ($relative -match '^(?i:template_build)[\\/]') {
        if ($relative -notin @('template_build\README.md','template_build\Export-GuestToolkit.ps1')) { return }
    }
    if ($relative -match '(?i)(^|[\\/])(__pycache__|tests|Guest-Agent-QuickFix)([\\/]|$)' -or
        $relative -match '(?i)\.(pyc|sqlite3|db|pem|pfx|key)$' -or
        $relative.Contains(':')) { return }
    $destination = Join-Path $Target $relative
    $parent = Split-Path -Parent $destination
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
    $script:copied++
}
Write-Host "VM Orchestrator v$releaseVersion installed: $copied files." -ForegroundColor Green
Write-Host 'Existing database, TLS/DPAPI keys, legacy .venv and all VM files are preserved.' -ForegroundColor Green
Write-Host 'Preparing system Python dependencies (no new .venv)...' -ForegroundColor Cyan
& (Join-Path $Target 'scripts\host\Install-HostPythonDeps.ps1')
# Keep the installed directory lean. Move ONLY known legacy distribution files to
# the secured upgrade backup. Never touch runtime DB, client data, TLS or unknown files.
if ($backup) {
    $obsolete = @(
        'Guest-Agent-QuickFix', 'tests', 'README-V1.4.0.md',
        'CHANGELOG.md', 'REFACTOR-REVIEW.md',
        'app\ui\ui.js', 'app\ui\business-admin.js',
        'app\ui\biz-workspace.js', 'app\ui\self-service-admin.js',
        'app\ui\remote-admin.js',
        'scripts\host\Start-All.ps1', 'scripts\host\Start-WebConsole.ps1',
        'scripts\host\Start-HostAgent.ps1', 'scripts\host\Start-GuestGateway.ps1',
        'scripts\host\Launch-GuestGateway.ps1', 'scripts\host\Probe-GuestGateway.py',
        'scripts\host\Test-TwoClones.ps1', 'scripts\host\Wait-VMJob.ps1',
        'scripts\maintain_release.py', 'MANIFEST.sha256', 'Start-GuestGateway.cmd', 'Archive-LegacyVenv.cmd', 'Check-Services.cmd',
        'Configure-GuestFirewall.cmd', 'Configure-GuestNetwork.cmd',
        'Configure-Host8765-Firewall.cmd', 'Copy-Business-Key.cmd',
        'Copy-Login-Key.cmd', 'Create-Win10-Template-Kit.cmd',
        'Disable-LoginAutoStart.cmd', 'Enable-LoginAutoStart.cmd',
        'Initialize-AdminAccount.cmd', 'Install-HostDependencies.cmd',
        'scripts\host\Archive-LegacyVenv.ps1', 'scripts\host\Check-All.ps1',
        'scripts\host\Check-GuestGateway.ps1',
        'scripts\host\Create-Win10-Template-Kit.ps1',
        'scripts\host\Register-Template.ps1',
        'Launch-VMConsole.cmd', 'Open-VMConsole.cmd',
        'README-V1.4.1.md', 'README-V1.4.2.md',
        '升级说明-v1.3.4.txt', 'docs\V0.9-README.md',
        'docs\UPGRADE-1.3.1.md', 'requirements-dev.txt',
        'Install-Host-BootTask.cmd',
        'scripts\host\Install-Host-BootTask.ps1',
        'scripts\host\Start-Host-Background.ps1',
        'Windows10-Template-Tools',
        'Windows10-Template-Tools\Apply-PerCloneOOBE.ps1',
        'Windows10-Template-Tools\Clear-KnownDshIdentity.ps1',
        'Windows10-Template-Tools\Finalize-OOBE.ps1',
        'Windows10-Template-Tools\FirstLogon-OOBE.ps1',
        'Windows10-Template-Tools\Get-OOBE-Status.ps1',
        'Windows10-Template-Tools\Inspect-Win10Template.ps1',
        'Windows10-Template-Tools\Install-OOBE-Bootstrap.ps1',
        'Windows10-Template-Tools\Prepare-OOBE-Base.ps1',
        'Windows10-Template-Tools\Seal-Win10Template.ps1'
    )
    # Archive known historical README files that the lean package no longer ships.
    # Never remove unknown user files: only exact versioned release notes and the
    # named historical CHANGES file are eligible for archival into this backup.
    $obsolete += 'CHANGES-1.4.3.txt'
    # Archive only known obsolete documentation files. Keep unknown user files.
    $obsolete += @(
        'integrations\app-report\README.md','integrations\sibhosting\README.md',
        'docs\CHANGELOG.md','docs\CLIENT-PORTAL-INTEGRATION.md',
        'docs\v1.4.34-refactor.md','docs\v1.4.35-changes.md',
        'docs\v1.4.36-fixes.md','docs\v1.4.36-long-commands-preview.md',
        'docs\v1.4.37-fixes.md','docs\v1.4.38-fixes.md',
        'docs\v1.4.39-merged-release.md','docs\v1.4.40-agent-updates.md',
        'docs\v1.4.42-update-recovery.md')
    $oldNotes = @(Get-ChildItem -LiteralPath $Target -File -Filter 'README-V1.4.*.md' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^README-V1\.4\.[0-9]+\.md$' -and $_.Name -ne 'README-V1.4.39.md' })
    foreach ($oldNote in $oldNotes) { $obsolete += $oldNote.Name }
    foreach ($oldRel in $obsolete) {
        $oldPath = Join-Path $Target $oldRel
        if (-not (Test-Path -LiteralPath $oldPath)) { continue }
        $archive = Join-Path $backup ('obsolete\' + $oldRel)
        $archiveParent = Split-Path $archive -Parent
        if (-not (Test-Path -LiteralPath $archiveParent)) {
            New-Item -ItemType Directory -Path $archiveParent -Force | Out-Null
        }
        Move-Item -LiteralPath $oldPath -Destination $archive -ErrorAction Stop
    }
}
if ($backup) { Write-Host "Backup: $backup" -ForegroundColor Cyan }
Write-Host "Installed to: $Target" -ForegroundColor Cyan
if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $Target 'Start-VM-Orchestrator.cmd') -WorkingDirectory $Target
}
