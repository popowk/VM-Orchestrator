# VMO Guest Agent V2: durable WSS results and safe protected-state upgrade.
# Run elevated in the Guest only. Existing enrollment credentials are migrated,
# NEVER regenerated. Legacy V1 scheduled task is REMOVED after V2 starts.
param([string]$PythonExe, [switch]$TemplateMode)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$admin = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator elevation required.' }

$program = 'C:\Program Files\VM-Orchestrator\GuestAgent'
$state = Join-Path $program 'state'
$legacy = Join-Path $env:ProgramData 'VMO'
$config = Join-Path $state 'agent.json'
$serviceName = 'VMO-GuestAgentV2'
$files = @('agent-release.json','agent.py','agent_v2.py','interactive_session.py','upload_directory.py','app_tunnel.py','ws_client.py','app_registry.py','local_app_report.py','update_lifecycle.py','dsh_updates.py','quick_updates.py','quick_plugin_update.py','update_commands.py','user_update_exec.py','Configure-ManagedUpdates.ps1','windows_service.py','machine_identity.py','Probe-Gateway.py','Test-AppEndpoints.ps1','Report-App-Startup.ps1','Report-App.cmd','Report-App-JSON.py','Set-RemotePermissions.ps1','Enable-RemoteCommands.cmd','Disable-RemoteCommands.cmd')

function Assert-ExitCode([string]$Operation) {
    if ($LASTEXITCODE -ne 0) { throw "$Operation failed (exit $LASTEXITCODE)" }
}
function Secure-ProgramDir {
    New-Item -ItemType Directory -Path $program -Force | Out-Null
    & icacls.exe $program /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
    Assert-ExitCode 'Program directory ACL'
}
function Secure-StateDir {
    New-Item -ItemType Directory -Path $state -Force | Out-Null
    # No Users permission on secrets, but ordinary Users can still run the code.
    & icacls.exe $state /inheritance:r /remove:g '*S-1-5-32-545' /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    Assert-ExitCode 'Protected state ACL'
    # Existing agent.json may have been created with a non-inheriting SYSTEM-only
    # ACL. Repair inherited children as well, without opening secrets to Users.
    & icacls.exe $state /remove:g '*S-1-5-32-545' /T /C | Out-Null
    Assert-ExitCode 'Remove inherited guest-user rights'
    & icacls.exe $state /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /T /C | Out-Null
    Assert-ExitCode 'Protected state files ACL'
}
function Pick-Python {
    if ($PythonExe) { return $PythonExe }
    $task = Get-ScheduledTask -TaskName 'VMO-GuestAgent' -ErrorAction SilentlyContinue
    if ($task -and $task.Actions) { return [string]$task.Actions[0].Execute }
    $svc = Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -ErrorAction SilentlyContinue
    if ($svc -and $svc.PathName -match '^"([^\"]+python(?:w)?\.exe)"') { return $Matches[1] }
    foreach ($p in @('C:\Program Files\Python314\python.exe','C:\Program Files\Python313\python.exe','C:\Program Files\Python312\python.exe','C:\Program Files\Python311\python.exe')) {
        if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    }
    throw 'System-wide Python 3.11+ not found.'
}
$python = Pick-Python
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or $python -match '\\Users\\|\\WindowsApps\\' -or $python.Contains('"')) {
    throw 'Invalid or per-user Python path.'
}
& $python -c 'import sys; assert sys.version_info >= (3, 11)'
Assert-ExitCode 'Python version verification'
foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $file) -PathType Leaf)) { throw "Package missing $file" }
}

$existingSvc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
$priorSvc = Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -ErrorAction SilentlyContinue
$previousBin = if ($priorSvc) { [string]$priorSvc.PathName } else { $null }
if ($priorSvc -and $priorSvc.StartName -notin @('LocalSystem','NT AUTHORITY\SYSTEM')) {
    throw 'Existing service is not running under SYSTEM. Refusing to replace it.'
}
Secure-ProgramDir
Secure-StateDir
# Back up code OUTSIDE state\backups: old ACLs there may prevent reading rollback files.
# The new backup folder is still protected, because agent.json contains enrollment secrets.
$backupRoot = Join-Path $program '_upgrade-backups'
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
& icacls.exe $backupRoot /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
Assert-ExitCode 'Upgrade backup directory ACL'
$backup = Join-Path $backupRoot ('upgrade-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
foreach ($file in $files) {
    $old = Join-Path $program $file
    if (Test-Path -LiteralPath $old -PathType Leaf) {
        Copy-Item -LiteralPath $old -Destination (Join-Path $backup $file) -Force -ErrorAction Stop
    }
}
if (Test-Path -LiteralPath $config -PathType Leaf) {
    Copy-Item -LiteralPath $config -Destination (Join-Path $backup 'agent.json') -Force -ErrorAction Stop
}
# Verify that we can actually read a backed-up file BEFORE touching the SCM service.
foreach ($file in $files) {
    $old = Join-Path $program $file
    $saved = Join-Path $backup $file
    if (Test-Path -LiteralPath $old -PathType Leaf) {
        if (-not (Test-Path -LiteralPath $saved -PathType Leaf)) { throw "Backup missing: $file" }
        $expected = (Get-FileHash -LiteralPath $old -Algorithm SHA256 -ErrorAction Stop).Hash
        $actual = (Get-FileHash -LiteralPath $saved -Algorithm SHA256 -ErrorAction Stop).Hash
        if ($expected -ne $actual) { throw "Backup checksum mismatch: $file" }
    }
}

# Migrate from legacy ProgramData ONCE and never overwrite an existing V2 identity.
if ($TemplateMode) {
    if ((Test-Path -LiteralPath $config -PathType Leaf) -or (Test-Path -LiteralPath (Join-Path $legacy 'agent.json') -PathType Leaf) -or (Test-Path -LiteralPath (Join-Path $state 'machine-binding.json') -PathType Leaf)) {
        throw 'Golden template contains a client identity. Remove it only after safely reviewing this BUILD VM.'
    }
} elseif (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    $oldConfig = Join-Path $legacy 'agent.json'
    if (-not (Test-Path -LiteralPath $oldConfig -PathType Leaf)) {
        throw 'No enrolled agent.json exists in the old or new location. Use the per-VM enrollment installer first.'
    }
    Copy-Item -LiteralPath $oldConfig -Destination $config -Force -ErrorAction Stop
    foreach ($item in @('host-only-ca.pem','app-registry.json','sync-status.json','pending-results.json')) {
        $p = Join-Path $legacy $item
        if (Test-Path -LiteralPath $p -PathType Leaf) { Copy-Item -LiteralPath $p -Destination (Join-Path $state $item) -Force }
    }
    if (Test-Path -LiteralPath (Join-Path $legacy 'logs') -PathType Container) {
        Copy-Item -LiteralPath (Join-Path $legacy 'logs') -Destination $state -Recurse -Force
    }
}
# Existing V2 state may still reference the legacy CA path; copy the CA if needed.
$caTarget = Join-Path $state 'host-only-ca.pem'
$oldCA = Join-Path $legacy 'host-only-ca.pem'
if (-not (Test-Path -LiteralPath $caTarget) -and (Test-Path -LiteralPath $oldCA -PathType Leaf)) {
    Copy-Item -LiteralPath $oldCA -Destination $caTarget -Force
}
if (Test-Path -LiteralPath $config -PathType Leaf) {
    $cfg = Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $cfg.vm_id -or -not $cfg.agent_id -or -not $cfg.agent_key -or -not $cfg.server_url -or -not $cfg.server_url.StartsWith('https://')) {
        throw 'Guest identity is not fully enrolled or HTTPS URL is invalid.'
    }
    $fromLegacy = Join-Path $legacy 'agent.json'
    if (Test-Path -LiteralPath $fromLegacy -PathType Leaf) {
        $old = Get-Content -LiteralPath $fromLegacy -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($old.vm_id -and $old.vm_id -ne $cfg.vm_id) { throw 'Refusing to mix credentials from two different VMs.' }
    }
    if (-not (Test-Path -LiteralPath $caTarget -PathType Leaf)) { throw 'Trusted CA missing; refusing to change the service.' }
    # Force the CA to the new protected location. Never change the CA itself.
    if ($cfg.PSObject.Properties['ca_bundle']) { $cfg.ca_bundle = $caTarget }
    else { $cfg | Add-Member -NotePropertyName 'ca_bundle' -NotePropertyValue $caTarget }
    $temp = Join-Path $state ('agent-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temp,($cfg | ConvertTo-Json -Depth 35),(New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temp -Destination $config -Force
    } finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force } }
}
Secure-StateDir
# Stop only AFTER all preflight/migration validation has succeeded.
# Never attempt to roll back files while the service is still RUNNING or STOP_PENDING.
$commitStarted = $false
$previouslyRunning = $existingSvc -and $existingSvc.Status -ne 'Stopped'
$phase = 'stop-service'
try {
    if ($existingSvc) {
        $current = Get-Service -Name $serviceName -ErrorAction Stop
        if ($current.Status -notin @('Stopped','StopPending')) {
            Stop-Service -Name $serviceName -ErrorAction Stop
        }
        $deadline = (Get-Date).AddSeconds(75)
        while ((Get-Date) -lt $deadline) {
            $current = Get-Service -Name $serviceName -ErrorAction Stop
            if ($current.Status -eq 'Stopped') { break }
            Start-Sleep -Milliseconds 500
        }
        $current = Get-Service -Name $serviceName -ErrorAction Stop
        if ($current.Status -ne 'Stopped') {
            throw ("Service is still {0} after 75 seconds. NO program files were replaced. Reboot the Guest, then run the fix again." -f $current.Status)
        }
    }

    $phase = 'copy-program-files'
    # From this point on, rollback is permitted only if SCM reports Stopped.
    $commitStarted = $true
    foreach ($file in $files) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $program $file) -Force -ErrorAction Stop
    }

    $phase = 'register-service'
    $bin = ('"{0}" -u "{1}" --service --config "{2}"' -f $python, (Join-Path $program 'agent_v2.py'), $config)
    if ($priorSvc) {
        $svc = Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -ErrorAction Stop
        $r = Invoke-CimMethod -InputObject $priorSvc -MethodName Change -Arguments @{PathName=$bin;StartMode='Automatic'} -ErrorAction Stop
        if ([int]$r.ReturnValue -ne 0) { throw "Service.Change returned $($r.ReturnValue)" }
    } else {
        New-Service -Name $serviceName -BinaryPathName $bin -DisplayName 'VMO Guest Agent V2' -StartupType Automatic -ErrorAction Stop | Out-Null
    }
    & sc.exe failure $serviceName 'reset=' '86400' 'actions=' 'restart/5000/restart/15000/restart/60000' | Out-Null

    $phase = 'start-service'
    Start-Service -Name $serviceName -ErrorAction Stop
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        $current = Get-Service -Name $serviceName -ErrorAction Stop
        if ($current.Status -eq 'Running') { break }
        if ($current.Status -eq 'Stopped') { throw 'Guest Agent service exited immediately after start.' }
        Start-Sleep -Milliseconds 500
    }
    if ((Get-Service $serviceName).Status -ne 'Running') { throw 'Guest Agent service did not reach Running within 45 seconds.' }
    Start-Sleep -Seconds 2
    if ((Get-Service $serviceName).Status -ne 'Running') { throw 'Guest Agent service did not remain Running.' }

    # Only now remove the legacy task. A failure to remove it must NOT roll back a running V2.
    $v1 = Get-ScheduledTask -TaskName 'VMO-GuestAgent' -ErrorAction SilentlyContinue
    if ($v1) {
        try {
            Unregister-ScheduledTask -TaskName 'VMO-GuestAgent' -Confirm:$false -ErrorAction Stop
        } catch {
            Write-Warning ('V2 is Running, but legacy V1 task could not be removed: ' + $_.Exception.Message)
        }
    }
    Write-Host 'SUCCESS: V2 automatic service is Running.' -ForegroundColor Green
    if (-not (Get-ScheduledTask -TaskName 'VMO-GuestAgent' -ErrorAction SilentlyContinue)) {
        Write-Host 'Legacy V1 task removed.' -ForegroundColor Green
    }
    Write-Host ('Protected Guest identity: ' + $config) -ForegroundColor Cyan
    Write-Host ('Verified pre-upgrade backups: ' + $backup) -ForegroundColor Cyan
    Write-Host 'WSS connectivity is a separate check; run Check-V2.cmd.' -ForegroundColor Yellow
} catch {
    $reason = $_.Exception.Message
    Write-Warning ("Upgrade stopped at $phase : $reason")
    $rollbackMessages = New-Object System.Collections.Generic.List[string]
    $now = Get-Service -Name $serviceName -ErrorAction SilentlyContinue

    if ($commitStarted) {
        # Keep old service stopped before restoring files.
        if ($now -and $now.Status -ne 'Stopped') {
            try { Stop-Service -Name $serviceName -ErrorAction Stop } catch { $rollbackMessages.Add('Stop for rollback failed: '+$_.Exception.Message) }
            $deadline=(Get-Date).AddSeconds(45)
            while ((Get-Date) -lt $deadline) {
                $now=Get-Service -Name $serviceName -ErrorAction SilentlyContinue
                if ($now -and $now.Status -eq 'Stopped') { break }
                Start-Sleep -Milliseconds 500
            }
        }
        $now=Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        if ($now -and $now.Status -ne 'Stopped') {
            $rollbackMessages.Add('Rollback of running files was SKIPPED: service is not stopped. Do not overwrite locked files.')
        } else {
            foreach ($file in $files) {
                $old = Join-Path $backup $file
                if (Test-Path -LiteralPath $old -PathType Leaf) {
                    try {
                        Copy-Item -LiteralPath $old -Destination (Join-Path $program $file) -Force -ErrorAction Stop
                    } catch {
                        $rollbackMessages.Add("Rollback failed for ${file}: " + $_.Exception.Message)
                    }
                }
            }
            if ($previousBin) {
                try {
                    $svc = Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -ErrorAction Stop
                    $r = Invoke-CimMethod -InputObject $svc -MethodName Change -Arguments @{PathName=$previousBin;StartMode='Automatic'} -ErrorAction Stop
                    if ([int]$r.ReturnValue -ne 0) { throw "Service.Change returned $($r.ReturnValue)" }
                } catch { $rollbackMessages.Add('Service configuration rollback failed: '+$_.Exception.Message) }
            }
        }
    }

    $now=Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($priorSvc -and $now -and $now.Status -eq 'Stopped' -and $rollbackMessages.Count -eq 0) {
        try { Start-Service -Name $serviceName -ErrorAction Stop }
        catch { $rollbackMessages.Add('Old service restart failed: '+$_.Exception.Message) }
    }
    foreach ($message in $rollbackMessages) { Write-Warning $message }
    Write-Warning ('Backups were retained at: ' + $backup)
    throw ("Guest Agent upgrade failed ($phase): $reason")
}
