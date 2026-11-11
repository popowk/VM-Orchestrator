# Runs as SYSTEM in Windows setup specialize pass on the newly cloned VM.
# Never prints XML or customer passwords. Fail closed to interactive OOBE if media missing.
$ErrorActionPreference = 'Stop'
$root = Join-Path $env:ProgramData 'VMO-OOBE'
New-Item -ItemType Directory -Path $root -Force | Out-Null
$logPath = Join-Path $root 'bootstrap.log'
function Log([string]$msg) { Add-Content -LiteralPath $logPath -Value ((Get-Date -Format 'o') + ' ' + $msg) -Encoding UTF8 }
try {
    $drive = $null
    for ($attempt=0; $attempt -lt 24; $attempt++) {
        $drives = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType = 5' -ErrorAction SilentlyContinue)
        $drive = $drives | Where-Object { $_.VolumeName -eq 'VMO_INIT' -and (Test-Path -LiteralPath ($_.DeviceID + '\Autounattend.xml')) -and (Test-Path -LiteralPath ($_.DeviceID + '\vmo-device.json')) } | Select-Object -First 1
        if ($drive) { break }
        Start-Sleep -Seconds 5
    }
    if (-not $drive) { throw 'VMO_INIT provisioning ISO not available during specialize.' }
    $isoRoot = $drive.DeviceID + '\'
    $manifest = Get-Content -LiteralPath (Join-Path $isoRoot 'vmo-device.json') -Encoding UTF8 -Raw | ConvertFrom-Json
    if ($manifest.schema -ne 1 -or $manifest.vm_id -notmatch '^[0-9a-fA-F-]{36}$' -or
        $manifest.hostname -notmatch '^VMO-[0-9A-F]{10}$' -or $manifest.username -ne 'Administrator') {
        throw 'Invalid per-clone manifest metadata.'
    }
    $xmlFile = Join-Path $isoRoot 'Autounattend.xml'
    [xml]$answer = Get-Content -LiteralPath $xmlFile -Raw -Encoding UTF8
    $ns = New-Object System.Xml.XmlNamespaceManager($answer.NameTable)
    $ns.AddNamespace('u','urn:schemas-microsoft-com:unattend')
    $localPwd = $answer.SelectSingleNode("/u:unattend/u:settings[@pass='oobeSystem']/u:component[@name='Microsoft-Windows-Shell-Setup']/u:UserAccounts/u:AdministratorPassword/u:Value", $ns)
    $autoUser = $answer.SelectSingleNode("/u:unattend/u:settings[@pass='oobeSystem']/u:component[@name='Microsoft-Windows-Shell-Setup']/u:AutoLogon/u:Username", $ns)
    if (-not $autoUser -or $autoUser.InnerText -ne 'Administrator' -or
        -not $localPwd -or $localPwd.InnerText.Length -lt 20) {
        throw 'Per-clone Administrator password/AutoLogon answer is missing.'
    }
    # Resolve by RID -500 to ensure this really is the built-in Administrator
    # and never a newly created same-named, less-privileged user account.
    $builtin = @(Get-LocalUser | Where-Object { $_.SID.Value -match '-500$' })
    if ($builtin.Count -ne 1 -or $builtin[0].Name -ne 'Administrator') {
        throw 'Built-in RID-500 account is missing/renamed; this lab template expects Administrator.'
    }
    $secret = ConvertTo-SecureString $localPwd.InnerText -AsPlainText -Force
    Set-LocalUser -Name 'Administrator' -Password $secret -ErrorAction Stop
    Enable-LocalUser -Name 'Administrator' -ErrorAction Stop
    $accountName = 'Administrator'
    Log 'Built-in RID-500 Administrator enabled with this clone unique password.' 
    # Automatic sign-in is temporary: FirstLogon-OOBE.ps1 disables and clears
    # these Winlogon values. ISO and local XML still contain the password until
    # post-install cleanup, and must never be included in the golden source.
    $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    New-ItemProperty -Path $winlogon -Name 'DefaultUserName' -PropertyType String -Value $accountName -Force | Out-Null
    New-ItemProperty -Path $winlogon -Name 'DefaultDomainName' -PropertyType String -Value $env:COMPUTERNAME -Force | Out-Null
    New-ItemProperty -Path $winlogon -Name 'DefaultPassword' -PropertyType String -Value $localPwd.InnerText -Force | Out-Null
    New-ItemProperty -Path $winlogon -Name 'AutoAdminLogon' -PropertyType String -Value '1' -Force | Out-Null
    New-ItemProperty -Path $winlogon -Name 'AutoLogonCount' -PropertyType DWord -Value 1 -Force | Out-Null
    Log 'Temporary one-time AutoLogon prepared.'
    $target = Join-Path $root 'PerClone.xml'
    Copy-Item -LiteralPath $xmlFile -Destination $target -Force
    & icacls.exe $target /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not secure per-clone answer file.' }
    # Request the unique hostname while Windows is still in specialize. Windows may
    # apply the name after its next setup reboot. Never fail OOBE just for this.
    try { Rename-Computer -NewName $manifest.hostname -Force -ErrorAction Stop; Log 'Unique computer rename requested.' }
    catch { Log ('Computer name will require a later reboot/check: ' + $_.Exception.Message) }
    # Keep answer as protected diagnostic provenance; DO NOT change Windows'
    # UnattendFile registry pointer mid-setup (unreliable/duplicate user creation).
    # The Sysprep-cached generic answer handles the oobeSystem pass.
    $metadata = [ordered]@{ vm_id=$manifest.vm_id; expected_hostname=$manifest.hostname; username=$manifest.username; staged_at=(Get-Date).ToUniversalTime().ToString('o'); stage='staged' }
    $metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'stage.json') -Encoding UTF8
    Log 'Per-clone account staged; base oobeSystem answer will handle regional and OOBE pages.'
    exit 0
} catch {
    Log ('STAGING FAILED: ' + $_.Exception.Message)
    exit 1
}
