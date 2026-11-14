# Host-side standalone exporter. Lives under scripts/host because legacy VMO
# update engines always copy scripts/, even when they omit template_build/.
[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$buildRoot=Join-Path $projectRoot 'template_build'
$guestSource=Join-Path $projectRoot 'scripts\guest'
$agentSource=Join-Path $projectRoot 'guest_agent'
$versionFile=Join-Path $projectRoot 'VERSION'
if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
    throw 'VMO project VERSION is missing; use this only on the Host.'
}
$version=(Get-Content -LiteralPath $versionFile -Raw -Encoding UTF8).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid VMO version' }
$requiredGuest=@('Install-OOBE-Bootstrap.ps1','Apply-PerCloneOOBE.ps1',
    'Finalize-OOBE.ps1','FirstLogon-OOBE.ps1','Get-OOBE-Status.ps1',
    'Prepare-TemplateGuest.ps1','Seal-Win10Template.ps1',
    'Check-TemplateStatus.ps1','Run-TemplateStep.ps1')
foreach($filename in $requiredGuest) {
    if (-not (Test-Path -LiteralPath (Join-Path $guestSource $filename) -PathType Leaf)) {
        throw ('Missing Guest Core source: '+$filename)
    }
}
$installer=Join-Path $agentSource 'Install-GuestAgentV2.ps1'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw 'Guest Agent V2 installer is missing'
}
# Always use the installation's own known-version Agent inventory, not a loose
# directory copy that might contain local state, configuration or credentials.
$installerText=Get-Content -LiteralPath $installer -Raw -Encoding UTF8
$match=[regex]::Match($installerText,'(?s)\$files\s*=\s*@\((.*?)\)')
if (-not $match.Success) { throw 'Cannot locate trusted Guest Agent V2 source inventory' }
$agentFiles=@([regex]::Matches($match.Groups[1].Value,"'([A-Za-z0-9_.-]+)'") |
    ForEach-Object { $_.Groups[1].Value })
$agentFiles+=@('Install-GuestAgentV2.ps1','Install-GuestAgentV2.cmd','Check-V2.ps1')
$agentFiles=@($agentFiles | Select-Object -Unique)
if ($agentFiles.Count -lt 10 -or $agentFiles.Count -gt 80) {
    throw 'Guest Agent source inventory count is invalid'
}
foreach ($filename in $agentFiles) {
    if ($filename -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,90}$') {
        throw 'Invalid Guest Agent filename'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $agentSource $filename) -PathType Leaf)) {
        throw ('Missing Guest Agent V2 source file: '+$filename)
    }
}
if (Test-Path -LiteralPath $buildRoot) {
    $buildItem=Get-Item -LiteralPath $buildRoot -Force
    if (-not $buildItem.PSIsContainer -or
        ($buildItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Unsafe template_build workspace; refusing export'
    }
} else { New-Item -ItemType Directory -Path $buildRoot -ErrorAction Stop | Out-Null }
$dest=Join-Path $buildRoot ('_GuestToolkit-v'+$version)
if (Test-Path -LiteralPath $dest) {
    throw ('Toolkit already exists; will NOT overwrite it: '+$dest)
}
New-Item -ItemType Directory -Path $dest -ErrorAction Stop | Out-Null
$core=Join-Path $dest 'Core'
$agent=Join-Path $dest 'Agent'
New-Item -ItemType Directory -Path @($core,$agent) -ErrorAction Stop | Out-Null
foreach($filename in $requiredGuest) {
    Copy-Item -LiteralPath (Join-Path $guestSource $filename) -Destination (Join-Path $core $filename) -ErrorAction Stop
}
foreach($filename in $agentFiles) {
    Copy-Item -LiteralPath (Join-Path $agentSource $filename) -Destination (Join-Path $agent $filename) -ErrorAction Stop
}
$cmdTemplate=@'
@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Core\Run-TemplateStep.ps1" -Action ACTION
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo [ERROR] This action failed or was cancelled. Exit code: %RESULT%
) else (
    echo [OK] This step completed. Please review the output.
)
pause
exit /b %RESULT%
'@
$actions=[ordered]@{
    '01-Prepare-And-Verify.cmd'='Prepare'
    '02-Verify-And-Seal.cmd'='Seal'
    '03-Check-Status.cmd'='Status'
}
foreach($item in $actions.GetEnumerator()) {
    $content=$cmdTemplate.Replace('ACTION',$item.Value)
    [IO.File]::WriteAllText((Join-Path $dest $item.Key),$content,[Text.Encoding]::ASCII)
}
$instructions=@'
VM Orchestrator - Windows 10 Golden Template Kit (BUILD VM ONLY)

This folder is produced on the VMware HOST. Copy the WHOLE folder into an
UNSEALED, DISPOSABLE Windows 10 BUILD VM, e.g. C:\dsh\VMO-Win10-Template-Kit.
NEVER execute 01/02/03 on the VMware host or an already enrolled customer VM.

01-Prepare-And-Verify.cmd
  Double-click. UAC will request Administrator. Installs generic OOBE hooks
  and identity-free VMO Guest Agent V2 as a Windows SYSTEM automatic service.
  Requires Windows, VMware Tools, and SYSTEM-WIDE Python 3.11+ installed first.
  Does not create any customer agent token, ID, password or account session.

03-Check-Status.cmd
  Read-only check for OOBE hooks, V2 service, and absence of enrolled identity.
  Run after step 01 and any time BEFORE Sysprep. It does not certify other app
  credentials or real first-boot success.

02-Verify-And-Seal.cmd
  Run only after reviewing application/browser/Windows credentials manually.
  Re-runs the read-only checks and requires you to type exactly SEAL.
  Then executes Sysprep /generalize /oobe /shutdown /mode:vm and shuts the
  BUILD VM down. Do not restart the sealed source.

After shutdown: detach the Windows installation ISO from the VMX, and use
Host admin Golden Templates to publish an official CANDIDATE by full clone.
Test OOBE, Guest Agent V2 registration and auto push with a new TEST customer VM.
Never copy real customer identities, DSH secrets or Windows account sessions
into the golden image. Do not delete or overwrite old templates/customer data.
'@
[IO.File]::WriteAllText((Join-Path $dest 'README-FIRST.txt'),$instructions,[Text.Encoding]::UTF8)
Write-Host ('READY: '+$dest) -ForegroundColor Green
Write-Host 'Copy the entire exported kit to the BUILD Windows guest, then double-click 01/03/02 in that order.'
Write-Host 'The existing official templates, customer data and BUILD VM disks were not modified.'
