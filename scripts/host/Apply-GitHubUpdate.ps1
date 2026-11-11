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
# Positive evidence that PowerShell successfully started this exact script.
# Created by the verified updater itself, not by an encoded wrapper command.
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$ackFile = Join-Path $stateDir ('updater-ack-' + $Attempt + '.txt')
[IO.File]::WriteAllText($ackFile, [string]$PID, [Text.Encoding]::ASCII)
$logFile = Join-Path $stateDir ('updater-' + $Attempt + '.log')
$folders = @('app','guest_agent','scripts','integrations','docs')
$rootFiles = @('VERSION','Install-All.cmd','Install-All.ps1','Start-VM-Orchestrator.cmd',
               'Manage.cmd','Repair-Stuck-Update.cmd','Repair-Stuck-Update.ps1',
               'README.md','requirements.txt','.gitignore','.env.example','release-manifest.json')
$backupDir = $null
$switched = $false
$stoppedOld = $false
$oldVersion = $null
# The detached updater acknowledges startup BEFORE checksum verification or
# process termination. Parser/launch failures remain in updater-bootstrap.log.

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
function Get-VerifiedVMOPid([string]$version,[int]$knownPid=0) {
    $hostPorts=@(Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue)
    $guestPorts=@(Get-NetTCPConnection -State Listen -LocalPort 8766 -ErrorAction SilentlyContinue)
    if ($hostPorts.Count -eq 0 -and $guestPorts.Count -eq 0) {
        if ($knownPid -gt 0 -and (Get-Process -Id $knownPid -ErrorAction SilentlyContinue)) {
            throw 'Host PID still exists after ports closed'
        }
        return 0
    }
    if ($hostPorts.Count -eq 0 -or $guestPorts.Count -eq 0) { throw 'VMO ports are not both listening' }
    $hostIds=@($hostPorts | Select-Object -ExpandProperty OwningProcess -Unique)
    $guestIds=@($guestPorts | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($hostIds.Count -ne 1 -or $guestIds.Count -ne 1 -or
        [int]$hostIds[0] -le 0 -or [int]$hostIds[0] -ne [int]$guestIds[0]) {
        throw 'VMO ports must belong to one PID'
    }
    $id=[int]$hostIds[0]
    if ($knownPid -gt 0 -and $id -ne $knownPid) { throw 'Host PID changed since update request' }
    $process=Get-CimInstance Win32_Process -Filter ("ProcessId=" + $id) -ErrorAction Stop
    if (-not $process -or $process.Name -notmatch '^python(w)?\.exe$') { throw 'Unknown VMO port owner' }
    if ($process.CommandLine -and $process.CommandLine -notmatch 'app\.unified_server') {
        throw 'Unexpected Python entrypoint'
    }
    $health=Invoke-RestMethod 'http://127.0.0.1:8765/health' -TimeoutSec 5 -ErrorAction Stop
    $disk=(Get-Content (Join-Path $Target 'VERSION') -Raw -ErrorAction Stop).Trim()
    if ($health.status -ne 'ok' -or $health.phase -ne 10 -or
        $health.capability -ne 'per-clone-oobe-template-bootstrap' -or
        [int]$health.pid -ne $id -or $health.version -ne $version -or $disk -ne $version) {
        throw 'VMO health or version mismatch'
    }
    $hostCheck=@(Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue |
                 Select-Object -ExpandProperty OwningProcess -Unique)
    $guestCheck=@(Get-NetTCPConnection -State Listen -LocalPort 8766 -ErrorAction SilentlyContinue |
                  Select-Object -ExpandProperty OwningProcess -Unique)
    if ($hostCheck.Count -ne 1 -or $guestCheck.Count -ne 1 -or
        [int]$hostCheck[0] -ne $id -or [int]$guestCheck[0] -ne $id) {
        throw 'VMO PID changed during identity check'
    }
    return $id
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
    # No new CMD window. Run under the logged-in VMware account.
    $file = Join-Path $Target 'scripts\host\Start-Unified.ps1'
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw 'Missing unified backend launcher'
    }
    $args = '-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
            $file + '" -NoBrowser'
    $out = Join-Path $stateDir 'host-launch-out.log'
    $err = Join-Path $stateDir 'host-launch-error.log'
    Start-Process -FilePath 'powershell.exe' -ArgumentList $args -WorkingDirectory $Target -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
}
function Get-LegacyVMOCmdId([int]$vmoPid) {
    if ($vmoPid -le 0) { return 0 }
    try {
        $py=Get-CimInstance Win32_Process -Filter ("ProcessId=" + $vmoPid) -ErrorAction Stop
        if (-not $py -or $py.Name -notmatch '^python(w)?\.exe$' -or $py.ParentProcessId -le 0) { return 0 }
        $parent=Get-CimInstance Win32_Process -Filter ("ProcessId=" + $py.ParentProcessId) -ErrorAction Stop
        if (-not $parent -or $parent.Name -notmatch '^(powershell|pwsh)\.exe$') { return 0 }
        $grand=Get-CimInstance Win32_Process -Filter ("ProcessId=" + $parent.ParentProcessId) -ErrorAction Stop
        if (-not $grand -or $grand.Name -ne 'cmd.exe') { return 0 }
        $p=Get-Process -Id $grand.ProcessId -ErrorAction Stop
        if ($grand.CommandLine -match 'Start-VM-Orchestrator\.cmd' -or
            $p.MainWindowTitle -eq 'VM Orchestrator - Unified Backend') {
            return [int]$grand.ProcessId
        }
    } catch { }
    return 0
}
function Close-OrphanedVMOLaunchers {
    # Older launchers are not always in Python's parent chain (for example,
    # a manual restart, a detached child, or a former Host instance).
    # Match only this project's uniquely named CMD title or script path.
    try {
        $all = @(Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction Stop)
        foreach ($item in $all) {
            $id = [int]$item.ProcessId
            if ($id -le 0) { continue }
            $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
            if (-not $proc) { continue }
            $line = [string]$item.CommandLine
            $titleMatch = ($proc.MainWindowTitle -eq 'VM Orchestrator - Unified Backend')
            $scriptMatch = ($line -match '(?i)Start-VM-Orchestrator\.cmd')
            if (-not ($titleMatch -or $scriptMatch)) { continue }
            # For a launcher with an explicit path, it must reside below Target.
            if ($scriptMatch -and -not $titleMatch -and
                $line -match '(?i)Start-VM-Orchestrator\.cmd' -and
                $line -match '(?i)[A-Z]:\\' -and
                $line -notlike ('*' + $Target + '*')) { continue }
            Stop-Process -Id $id -Force -ErrorAction Stop
            Log ('Closed stale VMO CMD PID ' + $id)
        }
    } catch { Log ('VMO launcher sweep skipped: ' + $_.Exception.Message) }
}
function Close-LegacyVMOCmd([int]$cmdId) {
    if ($cmdId -le 0) { return }
    try {
        $proc=Get-Process -Id $cmdId -ErrorAction Stop
        $meta=Get-CimInstance Win32_Process -Filter ("ProcessId=" + $cmdId) -ErrorAction Stop
        if ($meta.Name -eq 'cmd.exe' -and
            ($meta.CommandLine -match 'Start-VM-Orchestrator\.cmd' -or
             $proc.MainWindowTitle -eq 'VM Orchestrator - Unified Backend')) {
            Stop-Process -Id $cmdId -Force -ErrorAction Stop
            Log ('Closed verified legacy VMO CMD PID ' + $cmdId)
        }
    } catch { Log ('Legacy CMD closure skipped: ' + $_.Exception.Message) }
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
    $beforeVersion=(Get-Content (Join-Path $Target 'VERSION') -Raw -ErrorAction Stop).Trim()
    $oldId=Get-VerifiedVMOPid $beforeVersion $HostPid
    if ($oldId -gt 0) {
        $HostPid=$oldId
        $oldCmdId=Get-LegacyVMOCmdId $HostPid
        Log ('Verified old Host PID ' + $HostPid)
        Stop-Process -Id $HostPid -Force -ErrorAction Stop
        $stoppedOld=$true
        Close-LegacyVMOCmd $oldCmdId
        Close-OrphanedVMOLaunchers
    } else {
        Log 'HostPid=0: no listener, safely skipping process termination'
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
    Close-OrphanedVMOLaunchers
} catch {
    $errorMessage = $_.Exception.Message
    try { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
        Log ('ERROR: ' + $errorMessage) } catch { }

    if ($switched) {
        try {
            $newId=Get-VerifiedVMOPid $Version 0
            if ($newId -gt 0) {
                Stop-Process -Id $newId -Force -ErrorAction Stop
                Start-Sleep -Seconds 2
            }
            foreach ($port in @(8765,8766)) {
                if (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
                    throw ('Rollback blocked: port ' + $port + ' is occupied')
                }
            }
            Restore-OldCode
            Launch-Host
            if (Probe-Version $oldVersion 20) {
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
