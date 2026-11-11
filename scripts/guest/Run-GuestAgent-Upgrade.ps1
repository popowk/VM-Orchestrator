# Runs as a detached LocalSystem process started by the existing VMO Guest Agent.
# The archive was downloaded over certificate-verified HTTPS and SHA256 checked.
# Do not attempt to update the active service from inside its own worker.
param(
    [Parameter(Mandatory=$true)][string]$Stage,
    [Parameter(Mandatory=$true)][ValidatePattern('^2\.[0-9]+\.[0-9]+$')][string]$Version
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$serviceName = 'VMO-GuestAgentV2'
$program = 'C:\Program Files\VM-Orchestrator\GuestAgent'
$state = Join-Path $program 'state'
$upgradeRoot = Join-Path $state 'agent-updates'
$log = Join-Path $state 'logs\agent-upgrade.log'
$statusFile = Join-Path $state 'agent-update-status.json'
$source = Join-Path $Stage 'guest_agent'

function Write-Result([string]$Status, [string]$Reason) {
    $result = @{
        version = $Version
        status = $Status
        reason = $Reason
        updated_at = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json -Compress
    $temp = $statusFile + '.tmp'
    [IO.File]::WriteAllText($temp, $result, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temp -Destination $statusFile -Force
}

# The first CMD worker must be allowed to persist its result and receive WSS
# acknowledgement before the installer stops the Agent service.
Start-Sleep -Seconds 12
try {
    $stagePath = [IO.Path]::GetFullPath($Stage).TrimEnd('\')
    if (-not $stagePath.StartsWith($upgradeRoot + '\',[StringComparison]::OrdinalIgnoreCase)) {
        throw 'Upgrade staging path must be under protected Agent state'
    }
    # The entire archive was pinned by SHA256 before this runner was launched.
    # Check the signed-by-hash manifest and the Guest's runtime version file.
    $manifestPath = Join-Path $Stage 'release.json'
    $guestVersionPath = Join-Path $source 'agent-release.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $guestVersionPath -PathType Leaf)) {
        throw 'Upgrade package version manifest missing'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $guestRelease = Get-Content -LiteralPath $guestVersionPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.version -ne $Version -or $guestRelease.version -ne $Version) {
        throw 'Upgrade package version mismatch'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $source 'Install-GuestAgentV2.ps1') -PathType Leaf)) {
        throw 'Agent installation script missing from verified staging package'
    }
    if (-not (Get-Service -Name $serviceName -ErrorAction SilentlyContinue)) {
        throw 'Cannot update: existing VMO Guest Agent service not found'
    }
    New-Item -ItemType Directory -Path (Split-Path $log -Parent) -Force | Out-Null
    Write-Result 'APPLYING' 'Installer started'
    ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] Applying Guest Agent ' + $Version) |
        Out-File -FilePath $log -Append -Encoding UTF8
    # The existing installer makes hashed backups and performs rollback if its
    # service upgrade fails. Never touch protected state/agent.json or ca.pem.
    & (Join-Path $source 'Install-GuestAgentV2.ps1') *>> $log
    if (-not $?) { throw 'Guest Agent installer returned failure' }
    $svc = Get-Service -Name $serviceName -ErrorAction Stop
    if ($svc.Status -ne 'Running') { throw 'Service not running after installer' }
    Write-Result 'INSTALLED' 'Service running; waiting for WSS version heartbeat'
} catch {
    # The installer itself attempts rollback; the host must confirm the actual
    # version upon reconnect. Do not claim a successful update here.
    try {
        ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] FAILED ' + $_.Exception.GetType().Name + ' ' + (($_.Exception.Message -replace '[\r\n]',' ') -replace '[A-Za-z0-9_\-]{48,}', '[REDACTED]')) |
            Out-File -FilePath $log -Append -Encoding UTF8
        Write-Result 'FAILED' $_.Exception.GetType().Name
    } catch { }
    exit 1
}
