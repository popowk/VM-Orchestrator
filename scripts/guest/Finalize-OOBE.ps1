# Runs from Windows SetupComplete, after Windows OOBE processing and before first login.
# Marker is not sufficient alone for delivery; Guest Agent also checks Windows ImageState/account.
$ErrorActionPreference = 'Stop'
$root = Join-Path $env:ProgramData 'VMO-OOBE'
$logPath = Join-Path $root 'bootstrap.log'
function Log([string]$msg) { Add-Content -LiteralPath $logPath -Value ((Get-Date -Format 'o') + ' ' + $msg) -Encoding UTF8 }
try {
    $stageFile = Join-Path $root 'stage.json'
    if (-not (Test-Path -LiteralPath $stageFile)) { Log 'No per-clone stage marker; skipping.'; exit 0 }
    $stage = Get-Content -LiteralPath $stageFile -Raw | ConvertFrom-Json
    $account = Get-LocalUser -Name $stage.username -ErrorAction SilentlyContinue
    if (-not $account -or $account.SID.Value -notmatch '-500$') { Log 'Built-in Administrator missing after OOBE.'; exit 0 }
    # Rename safely. A reboot may be necessary for the expected hostname to take effect.
    $renameRequested = $false
    if ($env:COMPUTERNAME -ne $stage.expected_hostname) {
        try { Rename-Computer -NewName $stage.expected_hostname -Force -ErrorAction Stop; $renameRequested=$true }
        catch { Log ('Computer rename pending manual inspection: ' + $_.Exception.Message) }
    }
    $marker = [ordered]@{
        vm_id=$stage.vm_id; username=$stage.username; expected_hostname=$stage.expected_hostname
        step='setup_complete_account_created'; rename_requested=$renameRequested
        completed_at=(Get-Date).ToUniversalTime().ToString('o')
    }
    $marker | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'oobe-marker.json') -Encoding UTF8
    # The answer contains the VM's initial password; remove this Windows-local copy.
    $perClone = Join-Path $root 'PerClone.xml'
    if (Test-Path -LiteralPath $perClone) { Remove-Item -LiteralPath $perClone -Force }
    $ptr = (Get-ItemProperty -Path 'HKLM:\SYSTEM\Setup' -Name UnattendFile -ErrorAction SilentlyContinue).UnattendFile
    if ($ptr -eq $perClone) { Remove-ItemProperty -Path 'HKLM:\SYSTEM\Setup' -Name UnattendFile -ErrorAction SilentlyContinue }
    Log 'SetupComplete account check passed; cleartext staging answer removed. ISO must be detached from host after verification.'
} catch { Log ('FINALIZATION FAILED: ' + $_.Exception.Message) }
exit 0
