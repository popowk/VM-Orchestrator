# Read-only Guest Agent diagnosis; does not print enrollment tokens or agent keys.
$ErrorActionPreference = 'Continue'
$program = Join-Path $env:ProgramFiles 'VM-Orchestrator\GuestAgent\agent.py'
if ($env:ProgramW6432) { $program = Join-Path $env:ProgramW6432 'VM-Orchestrator\GuestAgent\agent.py' }
$data = Join-Path $env:ProgramData 'VMO'
$config = Join-Path $data 'agent.json'
$log = Join-Path $data 'logs\agent.log'
$installed = Test-Path -LiteralPath $program -PathType Leaf -ErrorAction SilentlyContinue
$legacy = Test-Path -LiteralPath (Join-Path $data 'agent.py') -PathType Leaf -ErrorAction SilentlyContinue
Write-Host '=== VMO Guest Agent diagnostics ===' -ForegroundColor Cyan
Write-Host "Program code in Program Files: $installed"
Write-Host "Legacy code remains in ProgramData: $legacy"
$task = Get-ScheduledTask -TaskName 'VMO-GuestAgent' -ErrorAction SilentlyContinue
if ($task) {
    Write-Host "Task state: $($task.State)"
    Write-Host "Task run-as: $($task.Principal.UserId)"
    Write-Host "Task executable: $($task.Actions[0].Execute)"
    Write-Host "Task arguments: $($task.Actions[0].Arguments)"
    $info = Get-ScheduledTaskInfo -TaskName 'VMO-GuestAgent' -ErrorAction SilentlyContinue
    if ($info) { Write-Host "Last task result: $($info.LastTaskResult); last run: $($info.LastRunTime); next run: $($info.NextRunTime)" }
} else {
    Write-Host 'Task: MISSING' -ForegroundColor Yellow
}
try {
    if (Test-Path -LiteralPath $config -PathType Leaf -ErrorAction Stop) {
        $cfg = Get-Content -LiteralPath $config -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
        if ($cfg.agent_id -and $cfg.agent_key) { Write-Host 'Enrollment config: registered key present (verify heartbeat in Host UI)' -ForegroundColor Green }
        elseif ($cfg.vm_id -and $cfg.enrollment_token) { Write-Host 'Enrollment config: pending one-time enrollment' -ForegroundColor Yellow }
        else { Write-Host 'Enrollment config: exists but not ready' -ForegroundColor Yellow }
        if ($cfg.server_url) {
            try {
                $uri = [Uri]$cfg.server_url
                Write-Host "Gateway: $($uri.Host):$($uri.Port)"
                $ping = Test-NetConnection -ComputerName $uri.Host -Port $uri.Port -WarningAction SilentlyContinue
                Write-Host "Gateway TCP reachable: $($ping.TcpTestSucceeded)"
            } catch { Write-Warning 'Gateway address/connection test failed' }
        }
    } else { Write-Host 'Enrollment config: MISSING (template or unregistered)' -ForegroundColor Yellow }
} catch { Write-Warning 'Cannot read protected configuration; re-run elevated as Administrator.' }
if (Test-Path -LiteralPath $log -PathType Leaf -ErrorAction SilentlyContinue) {
    Write-Host '--- Recent Agent log lines (secrets not printed by the agent) ---'
    Get-Content -LiteralPath $log -Tail 12 -ErrorAction SilentlyContinue
} else { Write-Host 'Agent log: not yet created' }

# V1.3.2: a task exit code 0 does not prove a successful heartbeat.
$statusFile = Join-Path $data 'sync-status.json'
try {
    if (Test-Path -LiteralPath $statusFile -PathType Leaf) {
        $st = Get-Content -LiteralPath $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Host ('Last sync OK: {0}; last attempt: {1}; last success: {2}' -f $st.sync_ok,$st.last_attempt_utc,$st.last_success_utc)
        if ($st.failure_code) { Write-Host ('Sync failure category: {0}' -f $st.failure_code) -ForegroundColor Yellow }
    } else { Write-Host 'Sync status not present yet. Run the Guest Agent once or wait for the periodic task.' -ForegroundColor Yellow }
} catch { Write-Warning 'Cannot inspect redacted sync status.' }

# Periodic execution and real heartbeat freshness are distinct from task ExitCode 0.
try {
    if ($st -and $st.last_success_utc) {
        $age = ([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($st.last_success_utc)).TotalMinutes
        Write-Host ('Last successful HTTPS heartbeat age: {0:N1} minutes' -f $age)
        if ($age -gt 5) { Write-Warning 'Heartbeat stale (>5 minutes): inspect scheduled task NextRunTime and its Operational events.' }
    }
} catch { }
