# Discover and stop ONLY processes owned by one C:\dsh\<program> installation.
# Executable path must be under the root, or a limited shell must contain an
# explicit invocation of the approved BAT/CMD launcher. Nothing else is killed.
param(
    [Parameter(Mandatory=$true)][ValidateSet('Inspect','Stop')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$ProgramRoot,
    [Parameter(Mandatory=$true)][string]$Launcher
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($ProgramRoot).TrimEnd('\')
if (-not $root.StartsWith('C:\dsh\', [StringComparison]::OrdinalIgnoreCase) -or
    $root.Substring(7).Contains('\') -or $root.Length -lt 8) {
    throw 'PORTABLE_DIRECTORY_OUTSIDE_APPROVED_ROOT'
}
$launcherPath = [IO.Path]::GetFullPath((Join-Path $root $Launcher))
if (-not $launcherPath.StartsWith($root + '\',[StringComparison]::OrdinalIgnoreCase)) {
    throw 'PORTABLE_LAUNCHER_OUTSIDE_APPROVED_ROOT'
}
$processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
$owned = New-Object 'System.Collections.Generic.HashSet[int]'
$allowed = @('cmd.exe','powershell.exe','pwsh.exe','node.exe','python.exe','pythonw.exe')
foreach($p in $processes){
    $exe = [string]$p.ExecutablePath
    $line = [string]$p.CommandLine
    $name = [string]$p.Name
    if ($p.ProcessId -eq $PID -or [int]$p.ProcessId -le 4) { continue }
    # This excludes unrelated programs referencing the folder in casual text.
    $within = $exe.StartsWith($root + '\',[StringComparison]::OrdinalIgnoreCase)
    $invocation = ($allowed -contains $name.ToLowerInvariant()) -and
        ($line.IndexOf($launcherPath,[StringComparison]::OrdinalIgnoreCase) -ge 0)
    if($within -or $invocation) { [void]$owned.Add([int]$p.ProcessId) }
}
# Descendant expansion requires a verified parent PID still in the snapshot.
$changed=$true
while($changed){
    $changed=$false
    foreach($p in $processes){
        if([int]$p.ProcessId -gt 4 -and -not $owned.Contains([int]$p.ProcessId) -and
           $owned.Contains([int]$p.ParentProcessId)){
            $name=[string]$p.Name
            if($name -in @('System','csrss.exe','wininit.exe','services.exe','lsass.exe','winlogon.exe','explorer.exe')){continue}
            [void]$owned.Add([int]$p.ProcessId)
            $changed=$true
        }
    }
}
if($Mode -eq 'Stop'){
    # Any disappearance is fine. If Stop-Process errors on a remaining PID,
    # refuse replacement; never kill a different process occupying a reused PID.
    foreach($pid_ in @($owned | Sort-Object -Descending)){
        $known = $processes | Where-Object { [int]$_.ProcessId -eq $pid_ } | Select-Object -First 1
        $fresh = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $pid_) -ErrorAction SilentlyContinue
        if(-not $fresh){continue}
        if($known.CreationDate -ne $fresh.CreationDate) {throw 'PROCESS_PID_REUSED_ABORT_UPDATE'}
        Stop-Process -Id $pid_ -Force -ErrorAction Stop
    }
    Start-Sleep -Milliseconds 600
    foreach($pid_ in @($owned)){
        if(Get-Process -Id $pid_ -ErrorAction SilentlyContinue){throw 'OWNED_PROGRAM_STILL_RUNNING'}
    }
}
# UTF8 JSON output; only PIDs and count, no process command lines/secrets.
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
@{running=($owned.Count -gt 0);count=$owned.Count;mode=$Mode} | ConvertTo-Json -Compress
