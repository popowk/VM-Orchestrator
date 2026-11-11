# Configure a LOCAL, explicit SnowLuma portable updater for this Guest only.
# This never installs packages, never touches app data, and never changes enrollment identity.
param([string]$ProgramDir)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$principal=New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run as administrator.' }
$config=Join-Path $env:ProgramFiles 'VM-Orchestrator\GuestAgent\state\agent.json'
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) { throw 'Registered Guest Agent config not found.' }
$cfg=Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json
$registered=$cfg.PSObject.Properties['agent_key']
if (-not $registered -or -not $registered.Value) { throw 'Guest must enroll first. Do not configure this in a golden template.' }
if (-not $ProgramDir) {
    $folders=@()
    if (Test-Path -LiteralPath 'C:\dsh' -PathType Container) {
        $folders=@(Get-ChildItem 'C:\dsh' -Directory -Filter 'SnowLuma*' -ErrorAction SilentlyContinue | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'launcher.bat') })
    }
    if ($folders.Count -eq 1) { $ProgramDir=$folders[0].FullName }
    else {
        Write-Host 'Found SnowLuma folders:'
        $folders | ForEach-Object { Write-Host ('  '+$_.FullName) }
        $ProgramDir=Read-Host 'Enter the full SnowLuma directory (must contain launcher.bat)'
    }
}
$root=[IO.Path]::GetFullPath($ProgramDir.Trim('"'))
if ($root -eq [IO.Path]::GetPathRoot($root)) { throw 'Refusing root folder.' }
if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'SnowLuma folder missing.' }
if (-not (Test-Path -LiteralPath (Join-Path $root 'launcher.bat') -PathType Leaf)) { throw 'launcher.bat is required in SnowLuma folder.' }
$folderItem=Get-Item -LiteralPath $root -Force
if ($folderItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse points or symlink install directories are not supported.' }
if ($root -match '(?i)\\Windows\\|\\ProgramData\\VMO\\|\\VM-Orchestrator\\GuestAgent\\') { throw 'Refusing system or Guest Agent directory.' }
Write-Host ('SnowLuma portable install: '+$root) -ForegroundColor Cyan
Write-Warning 'UPDATING REQUIRES CLOSING SnowLuma/QQ AND CHECKING BACKUPS. Updates may alter program files.'
$typed=Read-Host 'Type ENABLE-SNOWLUMA to enable this adapter on this client VM only'
if ($typed -cne 'ENABLE-SNOWLUMA') { throw 'Configuration cancelled.' }
$backup=Join-Path (Join-Path $env:ProgramFiles 'VM-Orchestrator\GuestAgent\state\backups') ('agent-before-snow-config-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.json')
New-Item -ItemType Directory -Force (Split-Path $backup -Parent) | Out-Null
Copy-Item -LiteralPath $config -Destination $backup -Force
# Preserve identity, CA, token state, and every unrelated application setting.
if (-not $cfg.PSObject.Properties['installations'] -or $null -eq $cfg.installations) {
    $cfg | Add-Member -MemberType NoteProperty -Name 'installations' -Value ([pscustomobject]@{}) -Force
}
$adapter=[pscustomobject]@{mode='portable';program_dir=$root;required_files=@('launcher.bat');preserve_relative=@('data','config','profiles','credentials','logs','user_data','storage','session');stop_process_names=@()}
$cfg.installations | Add-Member -MemberType NoteProperty -Name 'snowluma' -Value $adapter -Force
$temp=Join-Path (Split-Path $config -Parent) ('agent-snow-'+[guid]::NewGuid().ToString('N')+'.tmp')
try {
    $json=$cfg | ConvertTo-Json -Depth 25
    [IO.File]::WriteAllText($temp,$json,(New-Object Text.UTF8Encoding($false)))
    $null=& icacls.exe $temp /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Cannot secure temporary configuration' }
    Move-Item -LiteralPath $temp -Destination $config -Force
} finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force } }
Write-Host 'SnowLuma adapter configured. Wait for the next Guest Agent heartbeat; do not send an update until it reports portable mode.' -ForegroundColor Green
Write-Host 'The client must close SnowLuma/QQ before a ZIP update. Existing data files are preserved, but review compatibility and backup first.' -ForegroundColor Yellow
