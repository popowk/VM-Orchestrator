# Single entry for 01/02/03.cmd in the disposable Windows BUILD VM.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidateSet('Prepare','Seal','Status')][string]$Action)
$ErrorActionPreference='Stop'
function Test-Admin {
    $p=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
if (-not (Test-Admin)) {
    # UAC prompt for users who double-click a .cmd instead of running as admin.
    # The Action argument is an enum; it cannot contain injected shell syntax.
    $file=$MyInvocation.MyCommand.Path
    if ($file.Contains('"')) { throw 'Unsupported path contains a quotation mark' }
    $args='-NoProfile -ExecutionPolicy Bypass -File "'+$file+'" -Action '+$Action
    try {
        $p=Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $args -Wait -PassThru -ErrorAction Stop
        exit $p.ExitCode
    } catch {
        Write-Host ('UAC elevation declined or failed: '+$_.Exception.Message) -ForegroundColor Red
        exit 1
    }
}
try {
    $prepare=Join-Path $PSScriptRoot 'Prepare-TemplateGuest.ps1'
    $verify=Join-Path $PSScriptRoot 'Check-TemplateStatus.ps1'
    $seal=Join-Path $PSScriptRoot 'Seal-Win10Template.ps1'
    switch($Action) {
        'Prepare' {
            Write-Host 'Step 01: configure generic OOBE and install identity-free Guest Agent V2' -ForegroundColor Cyan
            & $prepare -ConfirmBuildClone
            & $verify
            Write-Host 'Step 01 completed. Review all accounts and applications before sealing.' -ForegroundColor Green
        }
        'Status' {
            Write-Host 'Step 03: read-only BUILD template status' -ForegroundColor Cyan
            & $verify
        }
        'Seal' {
            Write-Host 'Step 02: verify before irreversible Sysprep' -ForegroundColor Yellow
            & $verify
            Write-Host 'WARNING: This operation generalizes Windows and SHUTS DOWN this BUILD VM.' -ForegroundColor Yellow
            Write-Host 'Use this only inside a disposable, unsealed BUILD Windows VM.' -ForegroundColor Yellow
            $consent=Read-Host 'Type SEAL to confirm credentials reviewed and VM shutdown'
            if ($consent -cne 'SEAL') { throw 'Sealing cancelled; no Sysprep was invoked.' }
            & $seal -IAmOnDisposableClone -IReviewedAllAppAndWindowsCredentials -IUnderstandTheVmWillShutDown -UnattendedBootstrap
            Write-Host 'Sysprep process exited; verify VMware reports this BUILD VM powered off.' -ForegroundColor Green
        }
    }
    exit 0
} catch {
    Write-Host ('ERROR: '+$_.Exception.Message) -ForegroundColor Red
    exit 1
}
