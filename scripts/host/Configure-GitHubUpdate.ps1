# Configure public or private GitHub Release source. Token is DPAPI-encrypted to this Windows user.
param([string]$Repository='popowk/VM-Orchestrator',[switch]$ClearToken)
$ErrorActionPreference='Stop'
if ($Repository -notmatch '^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$') {
    throw 'Repository must be owner/name, e.g. popowk/VM-Orchestrator'
}
$dir=Join-Path $env:LOCALAPPDATA 'VM-Orchestrator'
New-Item -ItemType Directory -Path $dir -Force | Out-Null
$repoFile=Join-Path $dir 'github-update-repo.txt'
$tokenFile=Join-Path $dir 'github-update-token.dpapi'
[IO.File]::WriteAllText($repoFile,$Repository,[Text.UTF8Encoding]::new($false))
if ($ClearToken) {
    if (Test-Path -LiteralPath $tokenFile) { Remove-Item -LiteralPath $tokenFile -Force }
    Write-Host 'Removed this user DPAPI GitHub token.' -ForegroundColor Green
} else {
    Write-Host 'For a private repo, supply a fine-grained GitHub token with Contents: Read-only.'
    Write-Host 'Leave blank to keep the existing token or use public Releases.'
    $token=Read-Host -Prompt 'GitHub release read token (hidden)' -AsSecureString
    $plain=([System.Net.NetworkCredential]::new('',$token)).Password
    if ($plain) {
        if ($plain.Length -lt 20 -or $plain.Length -gt 300) { throw 'Token length is invalid' }
        ConvertFrom-SecureString $token | Set-Content -LiteralPath $tokenFile -Encoding ASCII
        Write-Host 'Token saved encrypted with Windows DPAPI.' -ForegroundColor Green
    } else { Write-Host 'Existing token unchanged (if configured).' }
}
Write-Host "Update source: $Repository" -ForegroundColor Cyan
Write-Host 'Restart Start-VM-Orchestrator.cmd after changing this setting.' -ForegroundColor Yellow
