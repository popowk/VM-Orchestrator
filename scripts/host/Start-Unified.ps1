# VMO 1.4.38 - single-process Host + Guest bootstrap.
# Run in the interactive Windows account that owns VMware Workstation.
param(
    [string]$TemplatesRoot = 'D:\VMware\Template',
    [string]$VmsRoot = 'D:\VMware\Customers',
    [string]$DatabasePath = 'D:\VMware\VM-Orchestrator\data\templates.sqlite3',
    [string]$VmrunPath = '',
    [int]$Port = 8765,
    [string]$PublicConsoleOrigin = 'https://host.tmd.pet',
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$project=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
. (Join-Path $PSScriptRoot 'Guest-Network.ps1')
$cfg=Get-GuestNetwork $project
Assert-GuestListenAddress $cfg.ListenIp
# Abort BEFORE touching stored secrets if any legacy listener still owns a port.
foreach ($listenPort in @($Port,[int]$cfg.Port)) {
    if (@(Get-NetTCPConnection -LocalPort $listenPort -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
        throw "Port $listenPort occupied. Stop all old Host/Gateway backends before starting unified mode."
    }
}
# Exact external HTTPS origin allowed to POST /ui/login behind ESA/DSM.
# Do not use a wildcard or trust arbitrary forwarded Host headers.
if (-not $env:VMO_PUBLIC_CONSOLE_ORIGIN) { $env:VMO_PUBLIC_CONSOLE_ORIGIN = $PublicConsoleOrigin }
Write-Host "Public Web Console origin: $env:VMO_PUBLIC_CONSOLE_ORIGIN" -ForegroundColor Cyan

# Optional GitHub Releases credential (not needed for public repositories), encrypted to the same interactive Windows user.
$githubDir=Join-Path $env:LOCALAPPDATA 'VM-Orchestrator'
$githubRepoFile=Join-Path $githubDir 'github-update-repo.txt'
$githubTokenFile=Join-Path $githubDir 'github-update-token.dpapi'
if (-not $env:VMO_UPDATE_REPO) {
    $env:VMO_UPDATE_REPO = if (Test-Path -LiteralPath $githubRepoFile) {
        (Get-Content -LiteralPath $githubRepoFile -Raw).Trim()
    } else { 'popowk/VM-Orchestrator' }
}
if ($env:VMO_UPDATE_REPO -notmatch '^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$') {
    throw 'Invalid configured GitHub repository'
}
if (-not $env:VMO_UPDATE_GITHUB_TOKEN -and (Test-Path -LiteralPath $githubTokenFile -PathType Leaf)) {
    try {
        $tokenEncrypted=(Get-Content -LiteralPath $githubTokenFile -Raw).Trim()
        $tokenSecure=ConvertTo-SecureString -String $tokenEncrypted
        $env:VMO_UPDATE_GITHUB_TOKEN=([System.Net.NetworkCredential]::new('', $tokenSecure)).Password
    } catch { throw 'Cannot decrypt GitHub update token for the current Windows user' }
}
Write-Host "GitHub update repository: $env:VMO_UPDATE_REPO" -ForegroundColor Cyan
# Prefer the existing DPAPI key: it keeps the API identity stable after restart.
$keyFile = Join-Path $env:LOCALAPPDATA 'VM-Orchestrator\admin-key.txt'
if (Test-Path -LiteralPath $keyFile -PathType Leaf) {
    try {
        $encrypted = (Get-Content -LiteralPath $keyFile -TotalCount 1).Trim()
        $secure = ConvertTo-SecureString -String $encrypted
        $env:VMO_ADMIN_KEY = ([System.Net.NetworkCredential]::new('', $secure)).Password
        if (-not $env:VMO_ADMIN_KEY -or $env:VMO_ADMIN_KEY.Length -lt 32) {
            throw 'Decrypted key is missing or too short'
        }
        Write-Host 'Loaded existing DPAPI management key.' -ForegroundColor Green
    } catch {
        throw "Cannot read $keyFile. Do not create a new key while another Host Agent is running. Detail: $($_.Exception.Message)"
    }
} elseif ($env:VMO_ADMIN_KEY -and $env:VMO_ADMIN_KEY.Length -ge 32) {
    $keyDir = Split-Path -Parent $keyFile
    New-Item -ItemType Directory -Path $keyDir -Force | Out-Null
    ConvertTo-SecureString $env:VMO_ADMIN_KEY -AsPlainText -Force |
        ConvertFrom-SecureString | Set-Content -LiteralPath $keyFile
    Write-Host 'Saved existing management key with Windows DPAPI.' -ForegroundColor Green
} else {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $env:VMO_ADMIN_KEY = [Convert]::ToBase64String($bytes)
    $keyDir = Split-Path -Parent $keyFile
    New-Item -ItemType Directory -Path $keyDir -Force | Out-Null
    ConvertTo-SecureString $env:VMO_ADMIN_KEY -AsPlainText -Force |
        ConvertFrom-SecureString | Set-Content -LiteralPath $keyFile
    Write-Host 'Generated a new management key, saved with Windows DPAPI.' -ForegroundColor Green
}

# Separate, stable Business API secret for trusted SibHosting server-to-server calls.
# Never reuse VMO_ADMIN_KEY and never send this key to customer browsers.
$businessKeyFile = Join-Path $env:LOCALAPPDATA 'VM-Orchestrator\business-key.txt'
if (Test-Path -LiteralPath $businessKeyFile -PathType Leaf) {
    $encryptedBusiness = (Get-Content -LiteralPath $businessKeyFile -TotalCount 1).Trim()
    $secureBusiness = ConvertTo-SecureString -String $encryptedBusiness
    $env:VMO_BUSINESS_KEY = ([System.Net.NetworkCredential]::new('', $secureBusiness)).Password
    if (-not $env:VMO_BUSINESS_KEY -or $env:VMO_BUSINESS_KEY.Length -lt 32) {
        throw 'Invalid saved business API key'
    }
} else {
    $businessBytes = New-Object byte[] 48
    $businessRng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $businessRng.GetBytes($businessBytes) } finally { $businessRng.Dispose() }
    $env:VMO_BUSINESS_KEY = [Convert]::ToBase64String($businessBytes)
    $businessDir = Split-Path -Parent $businessKeyFile
    New-Item -ItemType Directory -Path $businessDir -Force | Out-Null
    ConvertTo-SecureString $env:VMO_BUSINESS_KEY -AsPlainText -Force |
        ConvertFrom-SecureString | Set-Content -LiteralPath $businessKeyFile
    Write-Host 'Generated separate DPAPI-protected business API key.' -ForegroundColor Green
}

if (-not $VmrunPath) {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'VMware\VMware Workstation\vmrun.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'VMware\VMware Workstation\vmrun.exe')
    )
    $cmd = Get-Command vmrun.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $VmrunPath = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
        Select-Object -First 1
}
if (-not $VmrunPath -or -not (Test-Path -LiteralPath $VmrunPath -PathType Leaf)) {
    throw 'Could not locate vmrun.exe. Pass -VmrunPath with the exact installation path.'
}

Set-Location -LiteralPath $project
$BindAddress = if ($env:VMO_HOST_BIND) { $env:VMO_HOST_BIND } else { '0.0.0.0' }
$parsed = $null
if (-not [Net.IPAddress]::TryParse($BindAddress,[ref]$parsed) -or
    $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
    throw 'VMO_HOST_BIND must be valid IPv4.'
}
. (Join-Path $PSScriptRoot 'Resolve-HostPython.ps1')
$py=Get-VMOHostPython -Project $project
Assert-VMOHostDependencies $py
$tls=Join-Path $project 'data\guest-gateway-tls'
New-Item -ItemType Directory -Path $tls -Force | Out-Null
& $py (Join-Path $PSScriptRoot 'Create-GuestGatewayTLS.py') --ensure $cfg.ListenIp $tls $DatabasePath
if ($LASTEXITCODE -ne 0) { throw 'Guest TLS preparation failed; refusing to start a partially operating service.' }
$currentSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$keyGrant='*' + $currentSid + ':R'
$serverKey=Join-Path $tls 'server.key'
$aclResult=& icacls.exe $serverKey /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' $keyGrant 2>&1
if ($LASTEXITCODE -ne 0) { throw "Could not secure Guest TLS private key: $aclResult" }
foreach ($name in @('server.pem','ca.pem')) {
    $aclResult=& icacls.exe (Join-Path $tls $name) /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-32-545:R' 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Could not secure Guest TLS certificate $name" }
}
$gatewayUrl="https://$($cfg.ListenIp):$($cfg.Port)"
Set-Content -LiteralPath (Join-Path $tls 'gateway-url.txt') -Value $gatewayUrl -Encoding ASCII
$env:VMO_TEMPLATES_ROOT=$TemplatesRoot
$env:VMO_VMS_ROOT=$VmsRoot
$env:VMO_DB_PATH=$DatabasePath
$env:VMO_VMRUN_PATH=$VmrunPath
$env:VMO_GUEST_GATEWAY_URL=$gatewayUrl
$env:VMO_GUEST_CA_PEM=Join-Path $tls 'ca.pem'
Write-Host "VMO unified Python: $py" -ForegroundColor Green
Write-Host "Admin HTTP $BindAddress`:$Port / Guest HTTPS $($cfg.ListenIp):$($cfg.Port)" -ForegroundColor Cyan
Write-Warning 'Keep 8765 restricted to trusted admin IPs and 8766 restricted to guest subnet by Windows Firewall and iKuai ACL.'
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port/" }
& $py -m app.unified_server --host-bind $BindAddress --host-port $Port --guest-bind $cfg.ListenIp --guest-port $cfg.Port --certfile (Join-Path $tls 'server.pem') --keyfile $serverKey
if ($LASTEXITCODE -ne 0) { throw "VMO unified backend exited unexpectedly: $LASTEXITCODE" }
