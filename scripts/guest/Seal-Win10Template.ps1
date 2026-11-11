# Irreversible step on BUILD CLONE only. This does not validate external account cleanup.
param(
    [switch]$IAmOnDisposableClone,
    [switch]$IReviewedAllAppAndWindowsCredentials,
    [switch]$IReviewedSnowLumaData,
    [switch]$IUnderstandTheVmWillShutDown,
    [switch]$OmitVmMode,
    [switch]$UnattendedBootstrap,
    [switch]$LabOnlySkipDshIdentityCheck
)
$ErrorActionPreference = 'Stop'
if (-not ($IAmOnDisposableClone -and $IReviewedAllAppAndWindowsCredentials -and
          $IReviewedSnowLumaData -and $IUnderstandTheVmWillShutDown)) {
    throw 'All four explicit confirmations are required; use the README checklist first.'
}
$inspectScript = Join-Path $PSScriptRoot 'Inspect-Win10Template.ps1'
$check = & $inspectScript | ConvertFrom-Json
if ($check.blocked_count -gt 0) {
    $check.findings | Where-Object severity -eq 'BLOCK' | Format-Table
    if (-not $LabOnlySkipDshIdentityCheck) {
        throw "Refusing to seal: $($check.blocked_count) known blocking DSH data locations remain."
    }
    Write-Warning 'LAB ONLY: known app credentials and memory are still in this image. Never deliver this image to unrelated customers.'
}
# 32-bit PowerShell redirects System32; Sysnative addresses the real 64-bit Windows tree.
$sysprep = Join-Path $env:WINDIR 'System32\Sysprep\Sysprep.exe'
if (-not [Environment]::Is64BitProcess -and [Environment]::Is64BitOperatingSystem) {
    $sysprep = Join-Path $env:WINDIR 'Sysnative\Sysprep\Sysprep.exe'
}
if (-not (Test-Path -LiteralPath $sysprep)) { throw "Sysprep.exe missing at $sysprep; check Windows component health and PowerShell architecture" }
Write-Host 'Known DSH blocking paths not found. Operator attestations are NOT an automatic security guarantee.'
Write-Host 'Starting Sysprep. The VM will shut down if successful. DO NOT boot the sealed source afterward.'
$args = @('/generalize','/oobe','/shutdown')
if (-not $OmitVmMode) { $args += '/mode:vm' }
if ($UnattendedBootstrap) {
    $generic = Join-Path $env:ProgramData 'VMO-OOBE\Base-Unattend.xml'
    $stage = Join-Path $env:ProgramData 'VMO-OOBE\Apply-PerCloneOOBE.ps1'
    $finish = Join-Path $env:WINDIR 'Setup\Scripts\SetupComplete.cmd'
    if (-not ((Test-Path $generic) -and (Test-Path $stage) -and (Test-Path $finish))) {
        throw 'Unattended bootstrap is not staged. Run Install-OOBE-Bootstrap.ps1 on the BUILD COPY first.'
    }
    [xml]$unattend = Get-Content -LiteralPath $generic -Raw -Encoding UTF8
    $nodes = $unattend.SelectNodes("//*[local-name()='settings']")
    $passes = @($nodes | ForEach-Object { $_.GetAttribute('pass') })
    if ('specialize' -notin $passes -or 'oobeSystem' -notin $passes) {
        throw 'Invalid V1.2 answer: specialize and oobeSystem settings required. Re-run Install-OOBE-Bootstrap.ps1 from V1.2.'
    }
    $ns = New-Object System.Xml.XmlNamespaceManager($unattend.NameTable)
    $ns.AddNamespace('u','urn:schemas-microsoft-com:unattend')
    $account = $unattend.SelectSingleNode("/u:unattend/u:settings[@pass='oobeSystem']/u:component[@name='Microsoft-Windows-Shell-Setup']/u:AutoLogon/u:Username", $ns)
    if (-not $account -or $account.InnerText -ne 'Administrator') {
        throw 'Expected V1.2 Administrator AutoLogon in Base-Unattend.xml. Reinstall bootstrap from V1.2.'
    }
    $args += ('/unattend:' + $generic)
}
$process = Start-Process -FilePath $sysprep -ArgumentList $args -PassThru -Wait
if ($process.ExitCode -ne 0) { throw "Sysprep failed with exit code $($process.ExitCode). Inspect setupact.log/setuperr.log under Windows\System32\Sysprep\Panther." }
