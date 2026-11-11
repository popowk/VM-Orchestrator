# Run ONLY in a new template BUILD COPY prior to Sysprep. No passwords/tenant identity.
# This automates Windows 10 regional/privacy OOBE pages; account creation remains interactive.
param([string]$Output = 'C:\TemplateTools\unattend.xml')
$ErrorActionPreference = 'Stop'
$xml = @'
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <InputLocale>0804:00000804</InputLocale>
      <SystemLocale>zh-CN</SystemLocale>
      <UILanguage>zh-CN</UILanguage>
      <UserLocale>zh-CN</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
    </component>
  </settings>
</unattend>
'@
[void][xml]$xml
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Output) | Out-Null
[IO.File]::WriteAllText($Output, $xml, (New-Object Text.UTF8Encoding($false)))
Write-Host "已生成无客户凭据的 Windows 10 OOBE 基础应答文件：$Output" -ForegroundColor Green
Write-Host '注意：此应答文件不会自动创建 Windows 用户，因而不能消除所有 OOBE 页面。'
Write-Host '在新 BUILD 模板中使用 Sysprep /generalize /oobe /shutdown /unattend:<file> 后封存。'
Write-Host '不要对已封存或已交付客户虚拟机再次运行 Sysprep。'
