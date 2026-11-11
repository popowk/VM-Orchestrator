$ErrorActionPreference='Continue'
$name='VMO-GuestAgentV2'
Write-Host '=== VMO Guest Agent V2 ===' -ForegroundColor Cyan
Get-Service -Name $name -ErrorAction SilentlyContinue | Format-Table Name,Status,StartType
Write-Host '=== Service executable / account ===' -ForegroundColor Cyan
Get-CimInstance -ClassName Win32_Service -Filter "Name='$name'" -ErrorAction SilentlyContinue | Select-Object Name,State,StartMode,StartName,PathName | Format-List
Write-Host '=== Legacy V1 task (expected missing) ===' -ForegroundColor Cyan
Get-ScheduledTask -TaskName 'VMO-GuestAgent' -ErrorAction SilentlyContinue | Select-Object TaskName,State | Format-Table
Write-Host '=== Latest WSS logs ===' -ForegroundColor Cyan
$log=Join-Path $env:ProgramFiles 'VM-Orchestrator\GuestAgent\state\logs\agent-v2.log'
if (Test-Path -LiteralPath $log -PathType Leaf) { Get-Content -LiteralPath $log -Tail 15 }
else { Write-Host 'No V2 log yet. If SCM failed, check System event log.' }
Write-Host '=== Recent SCM errors for this service ===' -ForegroundColor Cyan
try {
  Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Service Control Manager';StartTime=(Get-Date).AddHours(-1)} -ErrorAction Stop |
    Where-Object { $_.Message -like '*VMO-GuestAgentV2*' -or $_.Message -like '*VMO Guest Agent V2*' } |
    Select-Object -First 8 TimeCreated,Id,Message | Format-List
} catch { Write-Warning 'SCM log unavailable (permissions or no events).' }

Write-Host '=== Installed Guest and identity check ===' -ForegroundColor Cyan
$state = Join-Path $env:ProgramFiles 'VM-Orchestrator\GuestAgent\state'
$cfg = Join-Path $state 'agent.json'
if (Test-Path $cfg) {
    try {
        $c = Get-Content -LiteralPath $cfg -Encoding UTF8 -Raw | ConvertFrom-Json
        Write-Host ('VM ID: ' + $c.vm_id)
        Write-Host ('Registered: ' + [bool]($c.agent_id -and $c.agent_key))
        Write-Host ('Machine binding exists: ' + (Test-Path (Join-Path $state 'machine-binding.json')))
    } catch { Write-Warning 'Cannot read protected identity; run elevated.' }
}

Write-Host '=== HTTPS gateway health (cert checked) ===' -ForegroundColor Cyan
$probe = Join-Path $env:ProgramFiles 'VM-Orchestrator\GuestAgent\Probe-Gateway.py'
$svc = Get-CimInstance Win32_Service -Filter "Name='VMO-GuestAgentV2'" -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $probe -PathType Leaf -ErrorAction SilentlyContinue) {
    if ($svc -and $svc.PathName -match '^"([^"]+python(?:w)?\.exe)"') {
        & $Matches[1] $probe
    } else {
        Write-Host 'Cannot locate service Python executable for TLS check'
    }
}

Write-Host '=== Durable operation result queue (no secret values printed) ===' -ForegroundColor Cyan
$outbox=Join-Path $state 'pending-results.json'
if (Test-Path -LiteralPath $outbox -PathType Leaf) {
  try {
    $pending=Get-Content -LiteralPath $outbox -Raw | ConvertFrom-Json
    $n= @($pending.PSObject.Properties).Count
    Write-Host ('Pending result acknowledgements: ' + $n)
  } catch { Write-Warning 'Durable result queue cannot be read; launch elevated.' }
} else { Write-Host 'Pending result acknowledgements: 0' }
