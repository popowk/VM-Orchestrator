# VMO Guest Agent installer for Windows PowerShell 5.1 and later.
# Installs immutable code in Program Files, and protects enrollment data in ProgramData.
# TemplateMode NEVER enrolls the machine or embeds any per-customer identity.
param(
    [string]$ConfigFile,
    [switch]$TemplateMode,
    [switch]$Repair,
    [string]$PythonExe
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Do not copy secrets or modify scheduled tasks unless elevated.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this installer as Administrator (elevated PowerShell).'
}
if ($TemplateMode -and $ConfigFile) { throw 'TemplateMode must not receive a customer config.' }
if ($TemplateMode -and $Repair) { throw 'Choose either TemplateMode or Repair, not both.' }

$pf = [Environment]::GetEnvironmentVariable('ProgramW6432')
if (-not $pf) { $pf = $env:ProgramFiles }
if (-not $pf) { throw 'Program Files location is unavailable.' }
$programDir = Join-Path $pf 'VM-Orchestrator\GuestAgent'
$dataDir = Join-Path $env:ProgramData 'VMO'
$configTarget = Join-Path $dataDir 'agent.json'
$caTarget = Join-Path $dataDir 'host-only-ca.pem'
$codeTarget = Join-Path $programDir 'agent.py'
$source = Join-Path $PSScriptRoot 'agent.py'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing installer payload: $source" }

function Set-SecureAcl([string]$Path, [switch]$AllowUsersRead) {
    $grants = @('*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F')
    if ($AllowUsersRead) { $grants += '*S-1-5-32-545:(OI)(CI)RX' }
    $result = & icacls.exe $Path /inheritance:r /grant:r $grants 2>&1
    if ($LASTEXITCODE -ne 0) { throw "ACL configuration failed: $Path ($result)" }
}

function Test-PythonVersion([string]$Exe) {
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe -PathType Leaf)) { return $false }
    if ($Exe -match '(?i)\\Users\\|\\WindowsApps\\') { return $false }
    try {
        $output = & $Exe -c 'import sys;print(sys.version_info[0],sys.version_info[1],sep=chr(46))' 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $ver = [version]($output | Select-Object -First 1)
        return ($ver -ge [version]'3.11')
    } catch { return $false }
}

function Find-SystemPython([string]$Explicit) {
    # Prefer an explicit verified system Python, then standard all-users locations.
    $paths = New-Object System.Collections.Generic.List[string]
    if ($Explicit) { [void]$paths.Add($Explicit) }
    foreach ($root in @($pf, ${env:ProgramFiles(x86)})) {
        if (-not $root -or -not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        foreach ($candidate in @(Get-ChildItem -LiteralPath $root -Directory -Filter 'Python*' -ErrorAction SilentlyContinue)) {
            $bin = Join-Path $candidate.FullName 'python.exe'
            if (Test-Path -LiteralPath $bin -PathType Leaf) { [void]$paths.Add($bin) }
        }
    }
    foreach ($registryRoot in @('HKLM:\SOFTWARE\Python\PythonCore','HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore')) {
        if (-not (Test-Path -LiteralPath $registryRoot)) { continue }
        foreach ($versionKey in @(Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue)) {
            $installKey = Join-Path $versionKey.PSPath 'InstallPath'
            if (-not (Test-Path -LiteralPath $installKey)) { continue }
            try {
                $regKey = Get-Item -LiteralPath $installKey
                $exe = $regKey.GetValue('ExecutablePath')
                if (-not $exe) {
                    $base = $regKey.GetValue('')
                    if ($base) { $exe = Join-Path $base 'python.exe' }
                }
                if ($exe) { [void]$paths.Add($exe) }
            } catch { }
        }
    }
    foreach ($cmd in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if ($cmd.Source) { [void]$paths.Add($cmd.Source) }
    }
    foreach ($p in @($paths | Select-Object -Unique)) {
        if (Test-PythonVersion $p) { return (Resolve-Path -LiteralPath $p).Path }
    }
    throw 'System-wide Python 3.11+ not found. Install Python for all users (e.g. C:\Program Files\Python314) or pass -PythonExe.'
}

$python = Find-SystemPython $PythonExe
Write-Host "Selected system Python: $python" -ForegroundColor Cyan

# Protect the credential directory BEFORE writing enrollment tokens. Never grant Users read access here.
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
Set-SecureAcl $dataDir
foreach ($sub in @('logs','updates','backups','working')) {
    $p = Join-Path $dataDir $sub
    New-Item -ItemType Directory -Path $p -Force | Out-Null
    Set-SecureAcl $p
}

if ($TemplateMode) {
    if (Test-Path -LiteralPath $configTarget) {
        throw 'Template contains agent.json. Remove enrolled identity only after confirming this is a disposable BUILD clone.'
    }
    if (Test-Path -LiteralPath $caTarget) {
        throw 'Template already contains a Guest CA. Inspect and remove clone-specific enrollment media before sealing.'
    }
} elseif ($Repair) {
    # Restore the program/task while preserving every existing enrollment field.
    if (-not (Test-Path -LiteralPath $configTarget -PathType Leaf)) {
        Write-Warning 'No enrollment config found. Code and scheduled task will be repaired, but Guest remains UNREGISTERED.'
    }
} else {
    if (-not $ConfigFile -or -not (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) {
        throw 'ThisGuest install requires its unique agent.json file. Download a per-VM ZIP from the host UI.'
    }
    $incoming = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $incoming.vm_id -or -not $incoming.server_url -or -not $incoming.server_url.StartsWith('https://') -or -not $incoming.enrollment_token) {
        throw 'Invalid per-VM enrollment configuration (vm_id, HTTPS server_url and token required).'
    }
    if (Test-Path -LiteralPath $configTarget -PathType Leaf) {
        $oldCfg = Get-Content -LiteralPath $configTarget -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($null -ne $oldCfg.PSObject.Properties['agent_key'] -and [bool]$oldCfg.PSObject.Properties['agent_key'].Value) {
            throw 'Guest is already enrolled. Use Repair-ThisGuest.cmd to preserve existing identity instead of overwriting it.'
        }
        if ($oldCfg.vm_id -and $oldCfg.vm_id -ne $incoming.vm_id) {
            throw 'VM identity mismatch; refusing to install a different customer enrollment over this machine.'
        }
    }
    $caFile = Join-Path (Split-Path -Parent $ConfigFile) 'host-only-ca.pem'
    if (-not (Test-Path -LiteralPath $caFile -PathType Leaf)) { throw 'Per-VM installer is missing the trusted Guest Gateway CA.' }
}

# Program installation: ordinary Users can READ/EXECUTE code, only SYSTEM/Admin can modify.
New-Item -ItemType Directory -Path $programDir -Force | Out-Null
Set-SecureAcl $programDir -AllowUsersRead
Copy-Item -LiteralPath $source -Destination $codeTarget -Force
# Protect existing files as well as directory defaults; never make code user-writable.
$aclResult = & icacls.exe $codeTarget /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-32-545:R' 2>&1
if ($LASTEXITCODE -ne 0) { throw "Unable to secure Guest Agent executable: $aclResult" }

if (-not $TemplateMode -and -not $Repair) {
    # Atomic credential write; source ZIP is deliberately NOT erased by the installer.
    $tmp = Join-Path $dataDir ('enroll-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        Copy-Item -LiteralPath $ConfigFile -Destination $tmp -Force
        $null = & icacls.exe $tmp /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F'
        if ($LASTEXITCODE -ne 0) { throw 'Unable to secure the Guest enrollment temp file.' }
        Move-Item -LiteralPath $tmp -Destination $configTarget -Force
        Copy-Item -LiteralPath $caFile -Destination $caTarget -Force
        $null = & icacls.exe $caTarget /inheritance:r /grant:r '*S-1-5-18:R' '*S-1-5-32-544:F'
        if ($LASTEXITCODE -ne 0) { throw 'Unable to secure Guest Gateway CA file.' }
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    }
}

# Build the task through the Windows Task Scheduler COM API. Do NOT import a
# hand-authored Task XML: Windows 10 rejected the earlier LogonType XML with
# HRESULT 0x80041318. COM emits the proper schema for that Windows version.
# Boot trigger + daily recurring trigger with a finite one-day repeat window.
# Earlier indefinite TIME repetition could remain queued or stop repeating on some Windows 10 builds.
$taskName = 'VMO-GuestAgent'
$taskArgs = '"' + $codeTarget + '" --config "' + $configTarget + '" --once'
$oldTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$oldTaskXml = $null
if ($oldTask) { $oldTaskXml = Export-ScheduledTask -TaskName $taskName -ErrorAction Stop }
$probeName = 'VMO-GuestAgent-Preflight'
$taskService = New-Object -ComObject 'Schedule.Service'
$taskService.Connect()
$taskFolder = $taskService.GetFolder('\')
$definition = $taskService.NewTask(0)
$definition.RegistrationInfo.Description = 'VMO Guest Agent: per-VM HTTPS sync at boot and every 2 minutes; --once.'
# TASK_LOGON_SERVICE_ACCOUNT=5, TASK_RUNLEVEL_HIGHEST=1, TASK_INSTANCES_IGNORE_NEW=2.
$definition.Principal.UserId = 'SYSTEM'
$definition.Principal.LogonType = 5
$definition.Principal.RunLevel = 1
$definition.Settings.Enabled = $true
$definition.Settings.StartWhenAvailable = $true
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.MultipleInstances = 2
$definition.Settings.ExecutionTimeLimit = 'PT10M'
$boot = $definition.Triggers.Create(8) # TASK_TRIGGER_BOOT
$boot.Enabled = $true
$timer = $definition.Triggers.Create(2) # TASK_TRIGGER_DAILY
$timer.DaysInterval = 1
$timer.StartBoundary = (Get-Date).AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')
$timer.Enabled = $true
$timer.Repetition.Interval = 'PT2M'
$timer.Repetition.Duration = 'P1D'
$timer.Repetition.StopAtDurationEnd = $false
$action = $definition.Actions.Create(0) # TASK_ACTION_EXEC
$action.Path = $python
$action.Arguments = $taskArgs
$action.WorkingDirectory = $programDir

function Assert-VmoTask([string]$Name) {
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
    if (-not $task.Actions -or $task.Actions[0].Execute -ne $python -or
        $task.Actions[0].Arguments -notlike '*GuestAgent*agent.py*--once*') {
        throw "Guest task validation failed: action mismatch ($Name)."
    }
    if ($task.Principal.UserId -notin @('SYSTEM','S-1-5-18','NT AUTHORITY\SYSTEM')) {
        throw "Guest task validation failed: not SYSTEM ($Name)."
    }
    if (@($task.Triggers).Count -lt 2 -or -not ($task.Triggers | Where-Object { $_.Repetition.Interval -eq 'PT2M' })) {
        throw "Guest task validation failed: missing periodic retry trigger ($Name)."
    }
}

# Preflight registration is separate from the existing task, so the original
# working task is left untouched if the new definition is not accepted.
# TASK_CREATE_OR_UPDATE=6; 5=TASK_LOGON_SERVICE_ACCOUNT.
try {
    $null = $taskFolder.RegisterTaskDefinition($probeName,$definition,6,'SYSTEM',$null,5)
    Assert-VmoTask $probeName
    if ($oldTask -and $oldTask.State -in @('Running','Queued')) {
        try { Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop }
        catch { Write-Warning 'Existing task could not be stopped; replacing its registration.' }
    }
    $null = $taskFolder.RegisterTaskDefinition($taskName,$definition,6,'SYSTEM',$null,5)
    try { Assert-VmoTask $taskName }
    catch {
        # The old XML came from Windows Export-ScheduledTask, not hand-written.
        if ($oldTaskXml) {
            try { Register-ScheduledTask -TaskName $taskName -Xml $oldTaskXml -Force -ErrorAction Stop | Out-Null }
            catch { Write-Warning 'Old task could not be restored automatically.' }
        }
        throw
    }
} finally {
    try { $taskFolder.DeleteTask($probeName,0) } catch { }
}
$registered = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
# Only remove the legacy CODE FILE after the new task is installed and verified.
$legacy = Join-Path $dataDir 'agent.py'
if (Test-Path -LiteralPath $legacy -PathType Leaf) {
    try { Remove-Item -LiteralPath $legacy -Force -ErrorAction Stop }
    catch { Write-Warning 'Old ProgramData agent.py could not be removed; the task now uses Program Files.' }
}
if (-not $TemplateMode) {
    # Repair-GuestAgent.ps1 performs a foreground --once check; avoid two
    # concurrent updates to the same enrollment/state files during repair.
    if (-not $Repair) { Start-ScheduledTask -TaskName $taskName -ErrorAction Stop }
}

Write-Host "Guest Agent code: $codeTarget" -ForegroundColor Green
Write-Host "Guest Agent task: $taskName (SYSTEM, boot + every 2 minutes, --once)" -ForegroundColor Green
if ($TemplateMode) { Write-Host 'TEMPLATE READY: no customer enrollment identity installed.' -ForegroundColor Yellow }
elseif (Test-Path -LiteralPath $configTarget) { Write-Host 'Guest Agent installed; enrollment and heartbeat may take time and require Gateway connectivity.' -ForegroundColor Cyan }
else { Write-Warning 'Guest Agent repaired but no agent.json exists. Generate and install the per-VM enrollment package.' }
