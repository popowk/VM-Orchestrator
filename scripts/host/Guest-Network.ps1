# Dot-source helpers. IPv4 is the host's OWN address, never an iKuai gateway address.
$script:GuestNetworkConfig = 'data\guest-network.json'
function Get-GuestNetwork {
    param([string]$Project)
    $configFile = Join-Path $Project $script:GuestNetworkConfig
    $conf = [pscustomobject]@{ ListenIp='192.168.2.99'; GuestSubnet='192.168.50.0/24'; Port=8766 }
    if (Test-Path -LiteralPath $configFile -PathType Leaf) {
        $settings = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($field in @('ListenIp','GuestSubnet','Port')) {
            if ($null -eq $settings.$field) { throw "Guest network config missing field: $field" }
        }
        $conf = $settings
    }
    $ip = [Net.IPAddress]::Parse([string]$conf.ListenIp)
    if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        $conf.ListenIp -eq '0.0.0.0' -or $conf.ListenIp -like '127.*' -or
        -not ($conf.ListenIp -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)')) {
        throw 'Guest Gateway ListenIp must be a specific private IPv4 address of the Windows host.'
    }
    if (-not ([string]$conf.GuestSubnet -match '^((?:\d{1,3}\.){3}\d{1,3})\/(\d|[12]\d|3[0-2])$')) {
        throw 'GuestSubnet must be IPv4 CIDR, e.g. 192.168.50.0/24'
    }
    $first = [Net.IPAddress]::Parse($matches[1])
    if ($first.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'Invalid GuestSubnet IPv4' }
    if ([int]$conf.Port -ne 8766) { throw 'Guest Gateway port is fixed at 8766 in this release.' }
    return $conf
}
function Assert-GuestListenAddress {
    param([string]$ListenIp)
    $addr = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $ListenIp -ErrorAction SilentlyContinue)
    if ($addr.Count -ne 1) {
        throw "Guest Gateway IP $ListenIp is not uniquely assigned to this Windows HOST. Use the host LAN IP (e.g. 192.168.2.99), never iKuai router 192.168.50.1."
    }
}
