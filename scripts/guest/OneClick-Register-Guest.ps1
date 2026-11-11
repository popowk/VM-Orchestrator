# Runs INSIDE a freshly cloned customer Windows VM, using its own per-VM installer ZIP.
# Elevated once; registers unique VM identity, waits for enrollment, then installs V2 auto-start service.
param([int]$EnrollmentWaitSeconds = 120)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$admin = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Right-click OneClick-Register-Guest.cmd -> Run as administrator.'
}
$cfgPath = Join-Path $PSScriptRoot 'agent.json'
$first = Join-Path $PSScriptRoot 'Install-GuestAgent.ps1'
$second = Join-Path $PSScriptRoot 'Install-GuestAgentV2.ps1'
foreach ($path in @($cfgPath,$first,$second)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing per-VM installer payload: $path" }
}
$source = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $source.vm_id -or -not $source.enrollment_token -or -not $source.server_url) {
  throw 'Missing per-VM identity / enrollment token. Download a fresh ZIP for this VM.'
}
$legacyCfg = Join-Path $env:ProgramData 'VMO\agent.json'
$v2Cfg = 'C:\Program Files\VM-Orchestrator\GuestAgent\state\agent.json'
function Get-Config([string]$Path) {
  if (Test-Path -LiteralPath $Path -PathType Leaf) {
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
  }
  return $null
}
# Properties may be absent until first enrollment; strict mode must not break bootstrap.
function Get-Field($Object, [string]$Name) {
  if ($null -eq $Object) { return $null }
  $property = $Object.PSObject.Properties[$Name]
  if ($null -eq $property) { return $null }
  return $property.Value
}
foreach ($path in @($legacyCfg,$v2Cfg)) {
  $old = Get-Config $path
  if ($old -and (Get-Field $old 'vm_id') -and (Get-Field $old 'vm_id') -ne $source.vm_id) {
    throw "Existing Guest identity belongs to a DIFFERENT VM ($($old.vm_id)); refusing to overwrite."
  }
}
$v2 = Get-Config $v2Cfg
if ($v2 -and (Get-Field $v2 'agent_id') -and (Get-Field $v2 'agent_key')) {
  $svc = Get-Service VMO-GuestAgentV2 -ErrorAction SilentlyContinue
  if ($svc -and $svc.Status -eq 'Running') {
    Write-Host 'Already registered with this VM; V2 service is running. No identity changes made.' -ForegroundColor Green
    exit 0
  }
  # Existing identical identity: V2 can safely repair the code and service.
  & $second
  if (-not $?) { throw 'V2 service repair failed.' }
  exit 0
}
$legacy = Get-Config $legacyCfg
if (-not ($legacy -and (Get-Field $legacy 'agent_id') -and (Get-Field $legacy 'agent_key'))) {
  Write-Host 'Stage 1/3: Installing unique Guest enrollment and boot agent...' -ForegroundColor Cyan
  & $first -ConfigFile $cfgPath
  if (-not $?) { throw 'Stage 1 install failed. Do not move this ZIP to another VM.' }
}
$until = (Get-Date).AddSeconds($EnrollmentWaitSeconds)
Write-Host 'Stage 2/3: Waiting for Agent enrollment via HTTPS/WSS 8766...' -ForegroundColor Cyan
while ((Get-Date) -lt $until) {
  $legacy = Get-Config $legacyCfg
  if ($legacy -and (Get-Field $legacy 'vm_id') -eq $source.vm_id -and (Get-Field $legacy 'agent_id') -and (Get-Field $legacy 'agent_key')) { break }
  Start-Sleep -Seconds 4
}
if (-not ($legacy -and (Get-Field $legacy 'vm_id') -eq $source.vm_id -and (Get-Field $legacy 'agent_id') -and (Get-Field $legacy 'agent_key'))) {
  throw 'Enrollment not confirmed. Check HTTPS 8766 reachability, Guest CA, and VMO-GuestAgent task. Retry with this SAME VM installer only.'
}
Write-Host 'Stage 3/3: Installing durable V2 Windows service (Automatic)...' -ForegroundColor Cyan
& $second
if (-not $?) { throw 'V2 install failed; Guest identity was retained for safe repair.' }
$svc = Get-Service VMO-GuestAgentV2 -ErrorAction Stop
if ($svc.Status -ne 'Running') { throw 'V2 service not running; inspect the Windows service/logs.' }
$installed = Get-Config $v2Cfg
if (-not $installed -or (Get-Field $installed 'vm_id') -ne $source.vm_id -or -not (Get-Field $installed 'agent_key')) {
  throw 'V2 enrolled identity verification failed.'
}
Write-Host ('SUCCESS: ' + $source.vm_id + ' registered; V2 starts automatically with Windows.') -ForegroundColor Green
Write-Host 'No Windows interactive login is needed for the Agent service.' -ForegroundColor Green
Write-Warning 'Delete the downloaded per-VM ZIP and extracted agent.json securely after successful enrollment; do not copy to the gold template.'
