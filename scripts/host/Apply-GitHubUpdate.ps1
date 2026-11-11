# VM Orchestrator detached self updater. Windows PowerShell 5.1 compatible.
# IMPORTANT: Runs outside the unified backend so it survives its termination.
param(
    [Parameter(Mandatory=$true)][string]$ZipPath,
    [Parameter(Mandatory=$true)][string]$ExpectedSha256,
    [Parameter(Mandatory=$true)][string]$Target,
    [Parameter(Mandatory=$true)][int]$HostPid,
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$Attempt = ''
)
$ErrorActionPreference = 'Stop'
$Target = [IO.Path]::GetFullPath($Target).TrimEnd('\')
$ZipPath = [IO.Path]::GetFullPath($ZipPath)
$stateDir = Join-Path $Target 'data\updates'
$statePath = Join-Path $stateDir 'status.json'
if (-not $Attempt) { $Attempt = [guid]::NewGuid().ToString('N') }
if ($Attempt -notmatch '^[0-9a-fA-F]{32}$') { throw 'Invalid update attempt identifier' }
$logFile = Join-Path $stateDir ('updater-' + $Attempt + '.log')
$folders = @('app','guest_agent','scripts','integrations','docs')
$rootFiles = @('VERSION','Install-All.cmd','Install-All.ps1','Start-VM-Orchestrator.cmd',
               'Manage.cmd','Repair-Stuck-Update.cmd','Repair-Stuck-Update.ps1',
               'README.md','requirements.txt','.gitignore','.env.example')
$backupDir = $null
$switched = $false
$stoppedOld = $false
$oldVersion = $null
# The detached updater must acknowledge its startup BEFORE attempting any
# archive extraction or process termination. This also makes parser errors
# visible in updater-bootstrap.log (stdout/stderr of powershell.exe).

function Log([string]$message) {
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $message
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
}
function Write-State([string]$status,[string]$description) {
    $data = @{state=$status;version=$Version;attempt=$Attempt;mode='restart';pid=$PID;message=$description;
              at=[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')}
    if ($backupDir) { $data['backup'] = $backupDir }
    if ($status -eq 'success' -and $script:VerifiedHostPid) {
        $data['verified_host_pid'] = $script:VerifiedHostPid
    }
    $json = $data | ConvertTo-Json -Compress -Depth 5
    $temp = $statePath + '.ps-' + $PID + '.tmp'
    [IO.File]::WriteAllText($temp,$json,[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $statePath -Force
    Log "$status : $description"
}
function Probe-Version([string]$needed,[int]$attempts=30) {
    $stable = 0
    $lastPid = 0
    $script:VerifiedHostPid = 0
    for ($i=0; $i -lt $attempts; $i++) {
        Start-Sleep -Seconds 3
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 2 -ErrorAction Stop
            $hostListener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
            $guestListener = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
            $freshPid = if ($hostListener) { [int]$hostListener.OwningProcess } else { 0 }
            $guestPid = if ($guestListener) { [int]$guestListener.OwningProcess } else { 0 }
            $diskVersion = (Get-Content -LiteralPath (Join-Path $Target 'VERSION') -Raw -ErrorAction Stop).Trim()
            # All checks must be valid across three consecutive probes. An old
            # service or a competing instance may briefly own a port.
            $pidMatches = (($needed -eq '') -or ($health.pid -and [int]$health.pid -eq $freshPid))
            $versionMatches = (($needed -eq '') -or
                               ($health.version -eq $needed -and $diskVersion -eq $needed))
            $healthy = ($health.status -eq 'ok' -and $freshPid -gt 0 -and
                        $freshPid -ne $HostPid -and $guestPid -eq $freshPid -and
                        $pidMatches -and $versionMatches)
            if ($healthy) {
                if ($freshPid -eq $lastPid) { $stable++ }
                else { $lastPid = $freshPid; $stable = 1 }
                if ($stable -ge 3) {
                    $script:VerifiedHostPid = $freshPid
                    return $true
                }
            } else {
                $stable = 0
                $lastPid = 0
            }
        } catch { $stable = 0; $lastPid = 0 }
    }
    return $false
}
function Launch-Host {
    $file = Join-Path $Target 'Start-VM-Orchestrator.cmd'
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw 'Missing unified backend launcher'
    }
    Start-Process -FilePath $file -WorkingDirectory $Target
}
function Replace-Code([string]$from) {
    foreach ($folder in $folders) {
        $old = Join-Path $Target $folder
        $new = Join-Path $from $folder
        if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
        if (Test-Path -LiteralPath $new -PathType Container) {
            Copy-Item -LiteralPath $new -Destination $old -Recurse -Force
        }
    }
    foreach ($name in $rootFiles) {
        $new = Join-Path $from $name
        $old = Join-Path $Target $name
        if (Test-Path -LiteralPath $new -PathType Leaf) {
            Copy-Item -LiteralPath $new -Destination $old -Force
        }
    }
}
function Restore-OldCode {
    if (-not $backupDir) { throw 'No backup available for rollback' }
    Replace-Code (Join-Path $backupDir 'code')
    # Files introduced by the failed release but not present before updating
    # are not part of an installed running process after directory restoration.
    if ($oldVersion) { [IO.File]::WriteAllText((Join-Path $Target 'VERSION'),$oldVersion+[Environment]::NewLine) }
}

try {
    if ($ExpectedSha256 -notmatch '^[a-fA-F0-9]{64}$' -or $Version -notmatch '^\d+\.\d+\.\d+$') {
        throw 'Invalid update package identity'
    }
    if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) { throw 'Update ZIP not found' }
    if (-not (Test-Path -LiteralPath (Join-Path $Target 'app\main.py'))) { throw 'Target not a VMO installation' }
    $actual = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) { throw 'SHA256 verification failed' }
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    Write-State 'preparing' ('Detached updater started (PID ' + $PID + ') and verifying release archive')
    $staging = Join-Path $stateDir ('unpacked-v' + $Version)
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('\','/')
            $prefix = 'VM-Orchestrator-v' + $Version + '/'
            if (-not $name.StartsWith($prefix,[StringComparison]::Ordinal) -or
                $name -match '(^/|(^|/)\.\.?(/|$)|:|//)') {
                throw 'ZIP contains disallowed path'
            }
        }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($ZipPath,$staging)
    $source = Join-Path $staging ('VM-Orchestrator-v' + $Version)
    if ((Get-Content -LiteralPath (Join-Path $source 'VERSION') -Raw).Trim() -ne $Version) {
        throw 'Release version does not match archive'
    }
    foreach ($required in @('app\main.py','scripts\host\Start-Unified.ps1','Start-VM-Orchestrator.cmd')) {
        if (-not (Test-Path -LiteralPath (Join-Path $source $required) -PathType Leaf)) {
            throw "Missing essential release file: $required"
        }
    }
    # Leave the HTTP request sufficient time to receive the accepted response.
    Start-Sleep -Seconds 4
    Write-State 'stopping' 'Release verified; stopping previous Host safely'
    $hostProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$HostPid" -ErrorAction SilentlyContinue
    if ($hostProcess -and ($hostProcess.Name -notmatch '^python(w)?\.exe$' -or
                          $hostProcess.CommandLine -notmatch 'app\.unified_server')) {
        throw 'Refusing to terminate a process which is not the VMO unified backend'
    }
    if ($hostProcess) {
        Stop-Process -Id $HostPid -Force -ErrorAction Stop
        $stoppedOld = $true
    }
    Start-Sleep -Seconds 2
    foreach ($port in @(8765,8766)) {
        if (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
            throw "Port $port is still occupied, refusing to replace a running service"
        }
    }
    Write-State 'backing_up' 'Service ports released; backing up prior code'
    $oldVersion = (Get-Content -LiteralPath (Join-Path $Target 'VERSION') -Raw).Trim()
    $backupDir = Join-Path $stateDir ('backups\before-v' + $Version + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $backupCode = Join-Path $backupDir 'code'
    New-Item -ItemType Directory -Path $backupCode -Force | Out-Null
    foreach ($folder in $folders) {
        $path = Join-Path $Target $folder
        if (Test-Path -LiteralPath $path -PathType Container) {
            Copy-Item -LiteralPath $path -Destination (Join-Path $backupCode $folder) -Recurse -Force
        }
    }
    foreach ($name in $rootFiles) {
        $path = Join-Path $Target $name
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Copy-Item -LiteralPath $path -Destination (Join-Path $backupCode $name) -Force
        }
    }
    Write-State 'applying' 'Backup complete; replacing program files'
    $switched = $true
    Replace-Code $source
    # Unlike a new system install, never initialize accounts, rotate keys,
    # touch the database, or run Sysprep/Guest provisioning here.
    Write-State 'applying' 'Code deployed; validating required Python dependencies'
    $deps = Join-Path $Target 'scripts\host\Install-HostPythonDeps.ps1'
    & $deps *> (Join-Path $stateDir 'dependencies.log')
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed' }
    Write-State 'restarting' 'New Host starting; awaiting health check'
    Launch-Host
    if (-not (Probe-Version $Version)) { throw 'New Host service did not pass the health check' }
    Write-State 'success' 'GitHub update succeeded; both Host ports are listening'
} catch {
    $errorMessage = $_.Exception.Message
    try { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
        Log ('ERROR: ' + $errorMessage) } catch { }

    if ($switched) {
        try {
            # Stop only the new VMO backend, if present.
            $newService = Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($newService) {
                $proc = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $newService.OwningProcess) -ErrorAction SilentlyContinue
                if ($proc -and $proc.Name -match '^python(w)?\.exe$' -and $proc.CommandLine -match 'app\.unified_server') {
                    Stop-Process -Id $proc.ProcessId -Force
                    Start-Sleep -Seconds 2
                }
            }
            Restore-OldCode
            Launch-Host
            if (Probe-Version '' 20) {
                Write-State 'rolled_back' ('Update failed; prior code restored: ' + $errorMessage)
            } else {
                Write-State 'failed' ('Update failed; prior Host not healthy after rollback: ' + $errorMessage)
            }
        } catch { Write-State 'failed' ('Rollback failed: ' + $_.Exception.Message) }
    } else {
        # If old service was stopped before the backup, restart unmodified code.
        try {
            if ($stoppedOld -and @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue).Count -eq 0) {
                Launch-Host
            }
        } catch { }
        Write-State 'failed' ('Update not completed; existing code unchanged: ' + $errorMessage)
    }
    exit 1
}
