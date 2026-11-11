# VMO Windows Host lifecycle controller (PowerShell 5.1).
# Only stops a verified VMO Python service, never guest VMware processes.
param(
    [Parameter(Mandatory=$true)][ValidateRange(1,2147483647)][int]$HostPid,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-fA-F]{32}$')][string]$Attempt
)
$ErrorActionPreference='Stop'
$project=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path.TrimEnd('\')
$data=Join-Path $project 'data\updates'
New-Item -ItemType Directory -Path $data -Force | Out-Null
$statusFile=Join-Path $data 'host-control-status.json'
$auditFile=Join-Path $data 'host-control.log'
$version=(Get-Content -LiteralPath (Join-Path $project 'VERSION') -Raw -Encoding UTF8).Trim()
$stdoutName='host-console-' + $Attempt + '-out.log'
$stderrName='host-console-' + $Attempt + '-err.log'

function Write-State([string]$state,[string]$message) {
    $record=@{state=$state; action='restart'; attempt=$Attempt
       expected_pid=$HostPid; version=$version; message=$message
       at=[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
       stdout_file=$stdoutName; stderr_file=$stderrName}
    $temporary=$statusFile + '.' + $PID + '.tmp'
    [IO.File]::WriteAllText($temporary,($record | ConvertTo-Json -Compress -Depth 4),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $statusFile -Force
    Add-Content -LiteralPath $auditFile -Value ((Get-Date -Format s) + ' [' + $state + '] ' + $message) -Encoding UTF8
}
function Verify-Host {
    $admin=@(Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    $guest=@(Get-NetTCPConnection -State Listen -LocalPort 8766 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    if($admin.Count -ne 1 -or $guest.Count -ne 1 -or [int]$admin[0] -ne $HostPid -or [int]$guest[0] -ne $HostPid){
        throw 'VMO ports 8765/8766 are not both owned by requested PID'
    }
    $process=Get-CimInstance Win32_Process -Filter ("ProcessId=" + $HostPid) -ErrorAction Stop
    if(-not $process -or $process.Name -notmatch '^python(w)?\.exe$'){throw 'Unexpected Host executable'}
    if($process.CommandLine -and $process.CommandLine -notmatch 'app\.unified_server'){throw 'Unexpected Host entrypoint'}
    $health=Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 3 -ErrorAction Stop
    if($health.status -ne 'ok' -or [int]$health.pid -ne $HostPid -or $health.version -ne $version -or
       $health.capability -ne 'per-clone-oobe-template-bootstrap'){throw 'Host PID or health mismatch'}
}
function Wait-PortsClosed {
    for($i=0;$i -lt 30;$i++){
        $admin=@(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
        $guest=@(Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue)
        if($admin.Count -eq 0 -and $guest.Count -eq 0){return}
        Start-Sleep -Milliseconds 500
    }
    throw 'Host ports are still occupied; refusing to start a competing Host'
}
function Launch-Host {
    $launcher=Join-Path $project 'scripts\host\Start-Unified.ps1'
    if(-not (Test-Path -LiteralPath $launcher -PathType Leaf)){throw 'Start-Unified.ps1 missing'}
    $args='-NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -WindowStyle Hidden -File "' + $launcher + '" -NoBrowser'
    $startParams=@{
        FilePath='powershell.exe'; ArgumentList=$args; WorkingDirectory=$project
        WindowStyle='Hidden'; ErrorAction='Stop'
        RedirectStandardOutput=(Join-Path $data $stdoutName)
        RedirectStandardError=(Join-Path $data $stderrName)
    }
    Start-Process @startParams | Out-Null
}
try{
    Write-State 'verifying' 'Checking VMO listener identity before stopping'
    Start-Sleep -Seconds 3
    Verify-Host
    Write-State 'stopping' 'Stopping only verified VMO Host; guest VMs remain running'
    Stop-Process -Id $HostPid -Force -ErrorAction Stop
    Wait-PortsClosed
    Write-State 'starting' 'Launching new Host in background with independent console logs'
    Launch-Host
    $stable=0;$lastPid=0
    for($i=0;$i -lt 45;$i++){
        Start-Sleep -Seconds 2
        try{
            $health=Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 2 -ErrorAction Stop
            $admin=@(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
            $guest=@(Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
            if($health.status -eq 'ok' -and $health.version -eq $version -and
               $admin.Count -eq 1 -and $guest.Count -eq 1 -and
               [int]$admin[0] -eq [int]$guest[0] -and
               [int]$health.pid -eq [int]$admin[0] -and [int]$health.pid -ne $HostPid){
                if([int]$health.pid -eq $lastPid){$stable++}else{$stable=1;$lastPid=[int]$health.pid}
                if($stable -ge 3){
                    Write-State 'running' ('Host ports 8765/8766 verified; PID=' + $lastPid)
                    exit 0
                }
            }else{$stable=0}
        }catch{$stable=0}
    }
    throw 'Host did not pass post-restart health checks; inspect console error log'
}catch{
    Write-State 'failed' ('Host lifecycle operation failed: ' + $_.Exception.Message)
    exit 1
}
