# Run on the customer Windows VM. Read-only connectivity diagnostic; no access codes printed.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Continue'
$apps = @(
  @{ Name = 'SnowLuma'; Port = 5099 },
  @{ Name = 'DSH-fish-soul'; Port = 56781 }
)
foreach ($app in $apps) {
  Write-Host "`n=== $($app.Name) : $($app.Port) ===" -ForegroundColor Cyan
  $listener = @(Get-NetTCPConnection -LocalPort $app.Port -State Listen -ErrorAction SilentlyContinue)
  if ($listener.Count -eq 0) {
    Write-Warning "No listener on port $($app.Port). The app's own web gateway may not have started."
    continue
  }
  foreach ($item in $listener) {
    Write-Host "Listening: $($item.LocalAddress):$($item.LocalPort) PID=$($item.OwningProcess)"
    Get-CimInstance Win32_Process -Filter "ProcessId=$($item.OwningProcess)" -ErrorAction SilentlyContinue |
      Select-Object Name,ProcessId,ExecutablePath | Format-List
  }
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$($app.Port)/" -UseBasicParsing -TimeoutSec 4
    Write-Host "HTTP: $($r.StatusCode) (authentication may still be required)"
  } catch {
    $status = $_.Exception.Response.StatusCode.value__
    if ($status) { Write-Warning "HTTP: $status (listener is reachable, check app authentication)" }
    else { Write-Warning "HTTP request failed: $($_.Exception.GetType().Name)" }
  }
}
Write-Host "`n=== Local application reporter ==="
try {
  $r=Invoke-RestMethod -Uri 'http://127.0.0.1:18767/health' -TimeoutSec 3
  $r | ConvertTo-Json -Compress
} catch { Write-Warning 'Guest local app reporter not reachable' }
Write-Host "`n=== Recent Guest WSS log ==="
$log='C:\Program Files\VM-Orchestrator\GuestAgent\state\logs\agent-v2.log'
if (Test-Path $log) { Get-Content $log -Tail 12 }
else { Write-Warning "Log not found: $log" }
