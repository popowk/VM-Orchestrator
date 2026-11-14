# Read-only safety check for an UNSEALED Windows BUILD VM.
# Never inspect/print private application credential contents.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
function Test-Admin {
    $p = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
$results = New-Object System.Collections.Generic.List[object]
function Report([string]$Name, [bool]$Pass, [string]$Detail) {
    $results.Add([pscustomobject]@{Check=$Name;Pass=$Pass;Detail=$Detail})
}
Report 'Administrator elevation' (Test-Admin) 'Must run in the Windows BUILD virtual machine as Administrator'
$root = Join-Path $env:ProgramData 'VMO-OOBE'
$base = Join-Path $root 'Base-Unattend.xml'
$required=@(
    $base,
    (Join-Path $root 'Apply-PerCloneOOBE.ps1'),
    (Join-Path $root 'Finalize-OOBE.ps1'),
    (Join-Path $root 'FirstLogon-OOBE.ps1'),
    (Join-Path $env:WINDIR 'Setup\Scripts\SetupComplete.cmd')
)
foreach($item in $required) {
    Report ('OOBE hook: '+(Split-Path -Leaf $item)) (Test-Path -LiteralPath $item -PathType Leaf) 'Prepared generic OOBE hook'
}
$validAnswer=$false
if(Test-Path -LiteralPath $base -PathType Leaf) {
    try {
        [xml]$answer=Get-Content -LiteralPath $base -Raw -Encoding UTF8
        $passes=@($answer.SelectNodes("//*[local-name()='settings']") | ForEach-Object {$_.GetAttribute('pass')})
        $ns=New-Object System.Xml.XmlNamespaceManager($answer.NameTable)
        $ns.AddNamespace('u','urn:schemas-microsoft-com:unattend')
        $account=$answer.SelectSingleNode("/u:unattend/u:settings[@pass='oobeSystem']/u:component[@name='Microsoft-Windows-Shell-Setup']/u:AutoLogon/u:Username",$ns)
        $validAnswer=('specialize' -in $passes -and 'oobeSystem' -in $passes -and
            $account -and $account.InnerText -eq 'Administrator')
    } catch {$validAnswer=$false}
}
Report 'OOBE specialize/oobeSystem answer' $validAnswer 'Generic template answer must target built-in Administrator'
$setupValid=$false
$setup=(Join-Path $env:WINDIR 'Setup\Scripts\SetupComplete.cmd')
if(Test-Path -LiteralPath $setup -PathType Leaf) {
    $setupValid=((Get-Content -LiteralPath $setup -Raw) -match 'VMO-OOBE\\Finalize-OOBE\.ps1')
}
Report 'SetupComplete VMO hook' $setupValid 'SetupComplete must run generic VMO OOBE finalizer'
$state = Join-Path $env:ProgramFiles 'VM-Orchestrator\GuestAgent\state'
foreach($file in @('agent.json','machine-binding.json','host-only-ca.pem')) {
    $present=Test-Path -LiteralPath (Join-Path $state $file)
    Report ('No customer identity: '+$file) (-not $present) 'The golden template must not carry customer identity or host CA'
}
$service=Get-CimInstance Win32_Service -Filter "Name='VMO-GuestAgentV2'" -ErrorAction SilentlyContinue
$serviceOk=($service -and $service.State -eq 'Running' -and $service.StartMode -eq 'Auto' -and
    $service.StartName -in @('LocalSystem','NT AUTHORITY\SYSTEM') -and
    $service.PathName -like '*GuestAgent*agent_v2.py*')
Report 'Guest Agent V2 auto SYSTEM service' $serviceOk 'Running identity-free V2 service; must wait for customer VMO_INIT ISO'
foreach($r in $results) {
    if($r.Pass){Write-Host ('[PASS] '+$r.Check) -ForegroundColor Green}
    else{Write-Host ('[FAIL] '+$r.Check+' -- '+$r.Detail) -ForegroundColor Red}
}
$failed=@($results | Where-Object {-not $_.Pass})
if($failed.Count) {
    throw ('Template is NOT ready for Sysprep ('+$failed.Count+' failed checks). Fix these items first.')
}
Write-Host 'PASS: Generic OOBE + identity-free Guest Agent V2 are ready for Sysprep.' -ForegroundColor Green
Write-Host 'Note: app/browser credentials still need manual review; this does not certify Windows OOBE on real hardware.'
