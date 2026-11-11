# Template BUILD COPY only. Run as Administrator before Sysprep.
# No customer name, password, token or Guest identity is embedded in the golden image.
[CmdletBinding()]
param([switch]$ConfirmBuildClone)
$ErrorActionPreference = 'Stop'
if (-not $ConfirmBuildClone) {
    throw 'This script changes the template build VM. Pass -ConfirmBuildClone on an unsealed BUILD COPY only.'
}
$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw 'Run PowerShell as Administrator inside the Windows 10 BUILD VM.' }
$root = Join-Path $env:ProgramData 'VMO-OOBE'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$stageSource = Join-Path $scriptDir 'Apply-PerCloneOOBE.ps1'
$finishSource = Join-Path $scriptDir 'Finalize-OOBE.ps1'
$firstLogonSource = Join-Path $scriptDir 'FirstLogon-OOBE.ps1'
if (-not (Test-Path $stageSource) -or -not (Test-Path $finishSource) -or -not (Test-Path $firstLogonSource)) {
    throw 'Copy the whole scripts\guest directory into this Windows build VM first.'
}
$setupScriptDir = Join-Path $env:WINDIR 'Setup\Scripts'
$setupComplete = Join-Path $setupScriptDir 'SetupComplete.cmd'
if (Test-Path -LiteralPath $setupComplete) {
    $existing = Get-Content -LiteralPath $setupComplete -Raw
    if ($existing -notmatch 'VMO-OOBE') {
        throw 'An existing SetupComplete.cmd was found. Merge manually; refusing to overwrite it.'
    }
}
New-Item -ItemType Directory -Path $root,$setupScriptDir -Force | Out-Null
Copy-Item -LiteralPath $stageSource -Destination (Join-Path $root 'Apply-PerCloneOOBE.ps1') -Force
Copy-Item -LiteralPath $finishSource -Destination (Join-Path $root 'Finalize-OOBE.ps1') -Force
Copy-Item -LiteralPath $firstLogonSource -Destination (Join-Path $root 'FirstLogon-OOBE.ps1') -Force
$xml = @'
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
 <settings pass="specialize">
  <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
   <RunSynchronous>
    <RunSynchronousCommand wcm:action="add">
     <Order>1</Order><Description>VMO per-clone OOBE answer selection</Description>
     <Path>powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\ProgramData\VMO-OOBE\Apply-PerCloneOOBE.ps1</Path>
     <WillReboot>Never</WillReboot>
    </RunSynchronousCommand>
   </RunSynchronous>
  </component>
  <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
   <ComputerName>*</ComputerName>
  </component>
 </settings>
 <settings pass="oobeSystem">
  <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
   <InputLocale>0804:00000804</InputLocale>
   <SystemLocale>zh-CN</SystemLocale>
   <UILanguage>zh-CN</UILanguage>
   <UserLocale>zh-CN</UserLocale>
  </component>
  <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
   <TimeZone>China Standard Time</TimeZone>
   <OOBE>
    <HideEULAPage>true</HideEULAPage>
    <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
    <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
    <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
    <ProtectYourPC>3</ProtectYourPC>
   </OOBE>
   <!-- The built-in Administrator is enabled and given a unique password from
        the per-clone ISO during specialize. This AutoLogon setting makes Windows
        10 skip the OOBE create-account page. NO password is stored in template. -->
   <AutoLogon>
    <Enabled>true</Enabled><Username>Administrator</Username><LogonCount>1</LogonCount>
   </AutoLogon>
   <FirstLogonCommands>
    <SynchronousCommand wcm:action="add">
     <Order>1</Order><Description>VMO first logon verification</Description>
     <CommandLine>powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\ProgramData\VMO-OOBE\FirstLogon-OOBE.ps1</CommandLine>
    </SynchronousCommand>
   </FirstLogonCommands>
  </component>
 </settings>
</unattend>
'@
[void][xml]$xml
[IO.File]::WriteAllText((Join-Path $root 'Base-Unattend.xml'), $xml, (New-Object System.Text.UTF8Encoding($false)))
$cmd = '@echo off' + "`r`n" + 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\ProgramData\VMO-OOBE\Finalize-OOBE.ps1"' + "`r`n" + 'exit /b 0' + "`r`n"
[IO.File]::WriteAllText($setupComplete, $cmd, [Text.Encoding]::ASCII)
# Restrict non-secret template-side scripts to SYSTEM/Administrators to reduce tampering.
& icacls.exe $root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict bootstrap folder ACL.' }
Write-Host 'V1.2 builtin Administrator specialize and oobeSystem bootstrap installed on BUILD COPY. No per-customer credentials included.' -ForegroundColor Green
Write-Host ('Run Seal-Win10Template.ps1 -UnattendedBootstrap after reviewing all app identities. DO NOT reboot after sealing.')
