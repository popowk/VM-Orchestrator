# Application startup hook. Works as Standard User, Administrator, or SYSTEM.
# Run only after the software has started and learned its actual URL/code.
param(
  [ValidateSet('snowluma','dsh-fish-soul')][string]$App,
  [string]$Url,
  [string]$Version = '',
  [switch]$CodeFromStdin,
  [string]$PythonExe='C:\Program Files\Python314\python.exe'
)
$ErrorActionPreference='Stop'
if (-not $App -or -not $Url) { throw 'Supply -App and -Url.' }
$tool='C:\Program Files\VM-Orchestrator\GuestAgent\Report-App-JSON.py'
if (-not (Test-Path -LiteralPath $tool)) { throw 'Guest Agent V2 JSON reporter not installed' }
$kind=if ($App -eq 'snowluma') {'random_password'} else {'token'}
$code=$null
if ($CodeFromStdin) { $code=[Console]::In.ReadLine() }
$payload=@{
  app_id=$App
  url=$Url
  version=$Version
  online=$true
  auth=@{kind=$kind;access_code=$code}
} | ConvertTo-Json -Compress -Depth 5
$payload | & $PythonExe $tool
$code=$null
$payload=$null
if ($LASTEXITCODE -ne 0) { throw 'Application report rejected. Check local Agent receiver.' }
Write-Host 'Application report accepted by Guest Agent. WSS sync will follow.' -ForegroundColor Green
