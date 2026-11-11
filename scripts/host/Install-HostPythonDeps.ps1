# Installs ONLY Python libraries for the current Windows login, never another Python runtime.
param([switch]$SkipInstall)
$ErrorActionPreference='Stop'
$project=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
. (Join-Path $PSScriptRoot 'Resolve-HostPython.ps1')
$py=Get-VMOHostPython -Project $project
Write-Host "VMO system Python: $py" -ForegroundColor Cyan
if (-not (Test-VMOHostDependencies $py)) {
    if ($SkipInstall) { throw 'Python dependencies missing; use Manage.cmd deps.' }
    Write-Host 'Installing dependencies to the current Windows user-site packages...' -ForegroundColor Cyan
    & $py -m pip --version
    if ($LASTEXITCODE -ne 0) { throw 'pip missing. Enable pip in the system Python installation.' }
    & $py -m pip install --user -r (Join-Path $project 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip failed. Retry Manage.cmd deps when the internet/package index is available.' }
}
Assert-VMOHostDependencies $py
$pin=Join-Path $project 'data\host-python.path'
New-Item -ItemType Directory -Path (Split-Path $pin -Parent) -Force | Out-Null
[IO.File]::WriteAllText($pin, $py + [Environment]::NewLine, [Text.Encoding]::UTF8)
Write-Host 'Host Python ready. No local .venv is created.' -ForegroundColor Green
