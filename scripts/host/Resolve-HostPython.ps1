# Choose an installed 64-bit Windows Python, NOT the old local .venv.
# Windows PowerShell 5.1 compatible. This is a function file (dot-source it).
function Get-VMOHostPython {
    param([Parameter(Mandatory=$true)][string]$Project)
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($env:VMO_HOST_PYTHON) { $candidates.Add($env:VMO_HOST_PYTHON) }
    $pin = Join-Path $Project 'data\host-python.path'
    if (Test-Path -LiteralPath $pin -PathType Leaf) {
        $pinned = (Get-Content -LiteralPath $pin -TotalCount 1).Trim()
        if ($pinned) { $candidates.Add($pinned) }
    }
    # Prefer a machine-wide installation over Python provided by DSH or other apps.
    foreach ($parent in @($env:ProgramFiles, (Join-Path $env:LOCALAPPDATA 'Programs\Python'))) {
        if (-not $parent -or -not (Test-Path -LiteralPath $parent -PathType Container)) { continue }
        Get-ChildItem -LiteralPath $parent -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^Python3\d{2}$' } |
            Sort-Object Name -Descending |
            ForEach-Object { $candidates.Add((Join-Path $_.FullName 'python.exe')) }
    }
    # Python.org's standard Windows installer records the interpreter in the registry.
    foreach ($regRoot in @('HKLM:\SOFTWARE\Python\PythonCore','HKCU:\SOFTWARE\Python\PythonCore')) {
        foreach ($key in @(Get-ChildItem -Path $regRoot -ErrorAction SilentlyContinue)) {
            $installKey = Join-Path $key.PSPath 'InstallPath'
            if (-not (Test-Path -LiteralPath $installKey)) { continue }
            $info = Get-ItemProperty -LiteralPath $installKey -ErrorAction SilentlyContinue
            if ($info -and $info.ExecutablePath) { $candidates.Add([string]$info.ExecutablePath) }
        }
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $launcherPath = & $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $launcherPath) { $candidates.Add(([string]@($launcherPath)[-1]).Trim()) }
        } catch { }
    }
    foreach ($cmd in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if ($cmd.Source) { $candidates.Add($cmd.Source) }
    }
    $visited = @{}
    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        $candidate = [Environment]::ExpandEnvironmentVariables($candidate.Trim('"'))
        if ($visited.ContainsKey($candidate.ToLowerInvariant())) { continue }
        $visited[$candidate.ToLowerInvariant()] = $true
        # Do not silently pick an app-private interpreter even when it appears in PATH.
        if ($candidate -match '(?i)[\\/](?:\.dsh|\.venv|venv|WindowsApps)[\\/]' -or
            $candidate -match '(?i)[\\/]DeepSeek Harness[\\/]') { continue }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        if ($candidate.StartsWith((Join-Path $Project '.venv'), [StringComparison]::OrdinalIgnoreCase)) { continue }
        try {
            $probe = & $candidate -c "import sys,struct; print('%d|%d|%d|%d' % (sys.version_info.major,sys.version_info.minor,struct.calcsize('P')*8, int(sys.prefix == sys.base_prefix)))" 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            $parts = ([string]@($probe)[-1]).Trim().Split('|')
            if ($parts.Count -ne 4) { continue }
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11 -and [int]$parts[2] -eq 64 -and [int]$parts[3] -eq 1) {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        } catch { continue }
    }
    throw 'Cannot find a 64-bit system Python 3.11+; install Python from python.org or set VMO_HOST_PYTHON to the full python.exe path.'
}
function Test-VMOHostDependencies {
    param([Parameter(Mandatory=$true)][string]$Python)
    try {
        & $Python -c 'import fastapi, uvicorn, pydantic, PIL, Crypto, pycdlib, cryptography, httpx, websockets' 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}
function Assert-VMOHostDependencies {
    param([Parameter(Mandatory=$true)][string]$Python)
    if (-not (Test-VMOHostDependencies $Python)) {
        throw 'Host Python dependencies are missing. Run Manage.cmd deps from this directory under the SAME Windows account, then retry.'
    }
}
