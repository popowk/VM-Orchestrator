# VMO v1.4.13 - local, opt-in per-VM update adapters. Run elevated in intended Windows user's session.
[CmdletBinding()]
param(
  [Parameter(Mandatory)][ValidateSet('SnowLuma','DshDesktop','DshPlugin')][string]$Adapter,
  [string]$ProgramDir = 'C:\dsh\SnowLuma-v2.4.0-win-x64',
  [string]$InstalledExe = 'C:\dsh\DeepSeek Harness\DeepSeek Harness.exe',
  [string]$DshCmd,
  [string]$PackageName,
  [string]$Profile = 'desktop',
  [string]$PublisherThumbprint,
  [string[]]$SilentArgs,
  [string[]]$BackupPaths,
  [string]$RestartTask,
  [switch]$RegisterStartTask
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$principal=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Please run as an administrator in the intended Windows user session.' }
$user=[Security.Principal.WindowsIdentity]::GetCurrent()
$targetSid=$user.User.Value
$targetAccount=$user.Name
$config=Join-Path $env:ProgramFiles 'VM-Orchestrator\GuestAgent\state\agent.json'
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {throw 'Enrolled Guest agent.json not found.'}
$cfg=Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $cfg.agent_id -or -not $cfg.agent_key -or -not $cfg.vm_id) {throw 'Guest is not enrolled. Never set an update adapter in a golden template.'}
if (-not $cfg.PSObject.Properties['installations'] -or $null -eq $cfg.installations) {
  $cfg | Add-Member -NotePropertyName installations -NotePropertyValue ([pscustomobject]@{}) -Force
}
function Assert-TaskName([string]$Name) {
  if ($Name -cnotmatch '^\\VMO-[A-Za-z0-9_-]{3,64}$') { throw 'Task name must match \\VMO-<name>.' }
}
function Assert-Backups([string[]]$Paths) {
  if (-not $Paths -or $Paths.Count -lt 1) {throw 'Specify -BackupPaths with the correct active user profile/config directories.'}
  foreach ($p in $Paths) {
    if (-not [IO.Path]::IsPathRooted($p) -or -not (Test-Path -LiteralPath $p)) {throw "Invalid backup path: $p"}
    if ([IO.Path]::GetFullPath($p) -eq [IO.Path]::GetPathRoot($p)) {throw 'Backing up a whole drive is not supported.'}
  }
}
function Register-InteractiveTask([string]$Name,[string]$Exe,[string]$Arguments,[string]$WorkingDir) {
  Assert-TaskName $Name
  if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) {throw "Launch executable missing: $Exe"}
  $act=New-ScheduledTaskAction -Execute $Exe -Argument $Arguments -WorkingDirectory $WorkingDir
  $who=New-ScheduledTaskPrincipal -UserId $targetAccount -LogonType Interactive -RunLevel Limited
  $trigger=New-ScheduledTaskTrigger -AtLogOn -User $targetAccount
  $settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName $Name.TrimStart('\') -Action $act -Principal $who -Trigger $trigger -Settings $settings -Force | Out-Null
}
$entry=$null
$product=$null
if ($Adapter -eq 'SnowLuma') {
  $product='snowluma'
  $ProgramDir=[IO.Path]::GetFullPath($ProgramDir)
  if (-not (Test-Path -LiteralPath (Join-Path $ProgramDir 'launcher.bat') -PathType Leaf)) {throw 'SnowLuma must contain launcher.bat.'}
  if ($ProgramDir -match '(?i)\\Windows\\|\\Program Files\\|\\VM-Orchestrator\\') {throw 'Refusing a system or Guest Agent directory.'}
  if (-not $RestartTask) {$RestartTask='\VMO-SnowLuma-Interactive'}
  Assert-TaskName $RestartTask
  if ($RegisterStartTask) {
    Register-InteractiveTask $RestartTask (Join-Path $env:SystemRoot 'System32\cmd.exe') '/D /K "call launcher.bat"' $ProgramDir
  }
  if (-not (Get-ScheduledTask -TaskName $RestartTask.TrimStart('\') -ErrorAction SilentlyContinue)) {throw 'First register the interactive start task with -RegisterStartTask, or provide an existing task.'}
  $entry=[pscustomobject]@{
    mode='portable_managed';program_dir=$ProgramDir;required_files=@('launcher.bat');
    preserve_relative=@('data','config','profiles','credentials','logs','user_data','storage','session');
    lifecycle=[pscustomobject]@{enabled=$true;port=5099;health_url='http://127.0.0.1:5099/';restart_task=$RestartTask}
  }
} elseif ($Adapter -eq 'DshDesktop') {
  $product='dsh'
  $InstalledExe=[IO.Path]::GetFullPath($InstalledExe)
  if (-not (Test-Path -LiteralPath $InstalledExe -PathType Leaf) -or [IO.Path]::GetFileName($InstalledExe) -ine 'DeepSeek Harness.exe') {throw 'Provide a real DeepSeek Harness.exe.'}
  if ($PublisherThumbprint -notmatch '^[A-Fa-f0-9]{40}$') {throw 'Provide the SHA1 certificate thumbprint of the verified vendor installer signing certificate.'}
  if (-not $SilentArgs -or $SilentArgs.Count -eq 0) {throw 'Provide explicit verified -SilentArgs for this installer family, e.g. only if vendor documents them.'}
  foreach($arg in $SilentArgs){if($arg -cnotmatch '^[-/A-Za-z0-9_=.:]{1,80}$'){throw 'Unsafe installer argument.'}}
  Assert-Backups $BackupPaths
  if (-not $RestartTask){$RestartTask='\VMO-DSH-Desktop'}
  Assert-TaskName $RestartTask
  if($RegisterStartTask){Register-InteractiveTask $RestartTask $InstalledExe '' (Split-Path -Parent $InstalledExe)}
  if (-not (Get-ScheduledTask -TaskName $RestartTask.TrimStart('\') -ErrorAction SilentlyContinue)) {throw 'Register the desktop restart task first.'}
  $entry=[pscustomobject]@{
    mode='desktop_installer';installed_executable=$InstalledExe;user_sid=$targetSid;
    signer_thumbprint=$PublisherThumbprint.ToUpperInvariant();silent_args=@($SilentArgs);
    backup_paths=@($BackupPaths);restart_task=$RestartTask;allow_desktop_installer=$true
  }
} else {
  $product='fish-soul'
  if (-not $DshCmd -or -not (Test-Path -LiteralPath $DshCmd -PathType Leaf)) {throw 'Provide the installed dsh.cmd or dsh.exe from the correct Desktop runtime.'}
  if($PackageName -cnotmatch '^(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]{0,127}$'){throw 'Specify the actual npm plugin package name, NOT app_id.'}
  if($Profile -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$'){throw 'Invalid DSH profile.'}
  Assert-Backups $BackupPaths
  if ($RestartTask){Assert-TaskName $RestartTask;if(-not (Get-ScheduledTask -TaskName $RestartTask.TrimStart('\') -ErrorAction SilentlyContinue)){throw 'Restart task is not registered.'}}
  $entry=[pscustomobject]@{
    mode='dsh_plugin_user';dsh_executable=[IO.Path]::GetFullPath($DshCmd);
    profile=$Profile;package_name=$PackageName;user_sid=$targetSid;
    backup_paths=@($BackupPaths);restart_task=$RestartTask;allow_user_profile_update=$true
  }
}
Write-Host "VM: $($cfg.vm_id) ; adapter: $product ; signed-in identity: $targetAccount ($targetSid)" -ForegroundColor Cyan
Write-Warning 'Only use approved versions and tested restart commands. Changes to customer data schemas may be irreversible.'
$confirm=Read-Host ("Type ENABLE-"+$product.ToUpperInvariant()+" to save this local adapter")
if ($confirm -cne ('ENABLE-'+$product.ToUpperInvariant())) {throw 'Cancelled.'}
$dir=Split-Path -Parent $config
$backupDir=Join-Path $dir 'backups'
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
Copy-Item -LiteralPath $config -Destination (Join-Path $backupDir ('agent-before-managed-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.json'))
$cfg.installations | Add-Member -MemberType NoteProperty -Name $product -Value $entry -Force
$temp=Join-Path $dir ('agent-update-'+[guid]::NewGuid().ToString('N')+'.tmp')
try {
  [IO.File]::WriteAllText($temp,($cfg | ConvertTo-Json -Depth 40),(New-Object Text.UTF8Encoding($false)))
  & icacls.exe $temp /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
  if ($LASTEXITCODE -ne 0) {throw 'Unable to secure temporary config.'}
  Move-Item -LiteralPath $temp -Destination $config -Force
} finally {if (Test-Path -LiteralPath $temp){Remove-Item -LiteralPath $temp -Force}}
Write-Host 'Adapter saved. Restart VMO-GuestAgentV2 and wait for heartbeat.' -ForegroundColor Green
