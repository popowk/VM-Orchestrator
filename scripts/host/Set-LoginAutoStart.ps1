# Register a shortcut under CURRENT USER's startup folder, not as SYSTEM.
param([switch]$Enable,[switch]$Disable)
$ErrorActionPreference='Stop'
if ($Enable -eq $Disable) { throw 'Specify exactly one of -Enable or -Disable.' }
$project=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$folder=[Environment]::GetFolderPath('Startup')
$link=Join-Path $folder 'VM Orchestrator (Host + Gateway).lnk'
if ($Enable) {
  $shell=New-Object -ComObject WScript.Shell
  $shortcut=$shell.CreateShortcut($link)
  $shortcut.TargetPath=Join-Path $project 'Start-VM-Orchestrator.cmd'
  $shortcut.WorkingDirectory=$project
  $shortcut.Description='Start Host Agent and Guest Gateway after this user logs on'
  $shortcut.Save()
  Write-Host "Login auto-start enabled for $env:USERNAME. Shortcut: $link" -ForegroundColor Green
} else {
  if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force }
  Write-Host 'VM Orchestrator login auto-start disabled.' -ForegroundColor Green
}
