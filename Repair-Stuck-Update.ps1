# One-time recovery for a stuck VMO Host self-update. Windows PowerShell 5.1.
# Run as the SAME logged-in Windows user that runs VMware/Host, not SYSTEM.
param(
    [string]$Target = 'D:\VMware\VM-Orchestrator',
    [string]$ZipPath = (Join-Path $PSScriptRoot 'VM-Orchestrator-v1.4.41.zip')
)
$ErrorActionPreference='Stop'
try {
    $Target = [IO.Path]::GetFullPath($Target)
    $ZipPath = [IO.Path]::GetFullPath($ZipPath)
    if (-not (Test-Path -LiteralPath (Join-Path $Target 'app\main.py') -PathType Leaf)) {
        throw "找不到 VMO 安装目录：$Target；请使用 -Target 指定实际位置"
    }
    if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
        throw "找不到更新包：$ZipPath"
    }
    $stateDir=Join-Path $Target 'data\updates'
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip=[IO.Compression.ZipFile]::OpenRead($ZipPath)
    $version=$null
    $runner=Join-Path $stateDir 'Repair-Updater.ps1'
    try {
        $verEntry=$zip.GetEntry('VM-Orchestrator-v1.4.41/VERSION')
        $scriptEntry=$zip.GetEntry('VM-Orchestrator-v1.4.41/scripts/host/Apply-GitHubUpdate.ps1')
        if (-not $verEntry -or -not $scriptEntry) { throw '更新包缺少 VERSION 或独立更新脚本' }
        $sr=New-Object IO.StreamReader($verEntry.Open())
        try { $version=$sr.ReadToEnd().Trim() } finally { $sr.Dispose() }
        if ($version -ne '1.4.41') { throw "不支持的修复包版本：$version" }
        $sourceStream=$scriptEntry.Open()
        $targetStream=[IO.File]::Open($runner,[IO.FileMode]::Create,[IO.FileAccess]::Write)
        try { $sourceStream.CopyTo($targetStream) } finally { $targetStream.Dispose();$sourceStream.Dispose() }
    } finally { $zip.Dispose() }
    $expected=(Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $shaPath=$ZipPath+'.sha256'
    if (Test-Path -LiteralPath $shaPath -PathType Leaf) {
        $line=(Get-Content -LiteralPath $shaPath -Raw).Trim()
        if ($line -notmatch '^([a-fA-F0-9]{64})\s+\*?VM-Orchestrator-v1\.4\.41\.zip$') {
            throw '不符合格式的 SHA256 清单'
        }
        if ($Matches[1].ToLowerInvariant() -ne $expected) { throw '更新包 SHA256 不匹配' }
    } else {
        Write-Warning '缺少发布版 .sha256 清单。建议优先从 GitHub Release 下载对应 checksum 文件。'
    }
    $hostPid=0
    $listener=Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $p=Get-CimInstance Win32_Process -Filter ("ProcessId="+$listener.OwningProcess) -ErrorAction Stop
        if (-not $p -or $p.Name -notmatch '^python(w)?\.exe$' -or
            $p.CommandLine -notmatch 'app\.unified_server') {
            throw '8765 已被非 VMO 进程占用，拒绝停止或覆盖。'
        }
        $hostPid=[int]$p.ProcessId
    }
    Write-Host "正在修复 VMO：$Target" -ForegroundColor Cyan
    Write-Host "更新版本：$version / 原服务 PID：$hostPid" -ForegroundColor Cyan
    Write-Host '将先备份现有程序；升级失败自动回滚。客户数据不删除。' -ForegroundColor Cyan
    & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner `
        -ZipPath $ZipPath -ExpectedSha256 $expected -Target $Target -HostPid $hostPid -Version $version
    if ($LASTEXITCODE -ne 0) { throw '独立更新器执行失败，请查看 data\updates\updater.log' }
    Write-Host '程序已启动且健康检查通过。' -ForegroundColor Green
} catch {
    Write-Host ('修复失败：'+$_.Exception.Message) -ForegroundColor Red
    exit 1
}
