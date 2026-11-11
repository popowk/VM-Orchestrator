# Runs once at the desktop after unattended OOBE. No password in logs.
# SetupComplete.cmd may be skipped on some Windows/OEM configurations,
# so this first-login hook also supplies an independent completion marker.
$ErrorActionPreference = 'Stop'
$root = Join-Path $env:ProgramData 'VMO-OOBE'
$log = Join-Path $root 'bootstrap.log'
function Log([string]$msg) { Add-Content -LiteralPath $log -Value ((Get-Date -Format 'o') + ' ' + $msg) -Encoding UTF8 }
try {
    # Disable automatic sign-in first, even if later verification fails.
    $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    New-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value '0' -PropertyType String -Force | Out-Null
    foreach ($name in @('DefaultPassword','AutoLogonCount','DefaultUserName','DefaultDomainName')) {
        Remove-ItemProperty -Path $winlogon -Name $name -ErrorAction SilentlyContinue
    }
    $stageFile = Join-Path $root 'stage.json'
    if (-not (Test-Path -LiteralPath $stageFile)) { throw 'No specialize stage marker' }
    $stage = Get-Content -LiteralPath $stageFile -Raw | ConvertFrom-Json
    if ($stage.username -ne 'Administrator' -or $stage.vm_id -notmatch '^[0-9a-fA-F-]{36}$') {
        throw 'Invalid per-clone first-login identity marker'
    }
    $account = Get-LocalUser -Name $stage.username -ErrorAction SilentlyContinue
    if (-not $account -or $account.SID.Value -notmatch '-500$') { throw 'Built-in Administrator missing after OOBE' }
    $marker = [ordered]@{
        vm_id=$stage.vm_id
        username=$stage.username
        expected_hostname=$stage.expected_hostname
        step='first_logon_complete'
        first_login_at=(Get-Date).ToUniversalTime().ToString('o')
    }
    $marker | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'oobe-marker.json') -Encoding UTF8
    # Clear the customer-specific answer left in Windows; host removes the ISO later.
    $perClone = Join-Path $root 'PerClone.xml'
    if (Test-Path -LiteralPath $perClone) { Remove-Item -LiteralPath $perClone -Force -ErrorAction SilentlyContinue }
    $ptr = (Get-ItemProperty -Path 'HKLM:\SYSTEM\Setup' -Name UnattendFile -ErrorAction SilentlyContinue).UnattendFile
    if ($ptr -eq $perClone) { Remove-ItemProperty -Path 'HKLM:\SYSTEM\Setup' -Name UnattendFile -ErrorAction SilentlyContinue }
    foreach ($answer in @((Join-Path $env:WINDIR 'Panther\Unattend.xml'), (Join-Path $env:WINDIR 'Panther\Unattend\Unattend.xml'))) {
        if (Test-Path -LiteralPath $answer) {
            try {
                $txt = Get-Content -LiteralPath $answer -Raw -ErrorAction Stop
                if ($txt.Contains('<AdministratorPassword>')) { Remove-Item -LiteralPath $answer -Force -ErrorAction Stop }
            } catch { Log 'Cached answer could not be removed; review before customer delivery.' }
        }
    }
    Log 'First desktop login completed; automatic sign-in disabled; OOBE marker stored.'
} catch { Log ('First logon verification failed: ' + $_.Exception.Message) }
