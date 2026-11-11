# Publish audited VM Orchestrator source to the *existing public* repository.
# Requires Git for Windows, GitHub CLI, and gh auth login. No access tokens in files.
param(
    [string]$Repository='popowk/VM-Orchestrator',
    [switch]$SkipReleaseTag,
    [switch]$PublicApproved
)
$ErrorActionPreference='Stop'
if ($Repository -notmatch '^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$') {
    throw 'Invalid owner/repository'
}
$root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root
foreach ($program in @('git.exe','gh.exe')) {
    if (-not (Get-Command $program -ErrorAction SilentlyContinue)) { throw "Install $program first" }
}
& gh.exe auth status *> $null
if ($LASTEXITCODE -ne 0) { throw 'Run gh auth login first' }
$me=(& gh.exe api user --jq '.login').Trim()
if ($LASTEXITCODE -ne 0 -or $Repository.Split('/')[0] -ne $me) {
    throw 'Only the authenticated repository owner may publish'
}
# Never accidentally initialize or push to another organization/repository.
$visibility=(& gh.exe repo view $Repository --json visibility --jq '.visibility' 2>$null)
if ($LASTEXITCODE -ne 0) { throw "Repository $Repository not accessible. Create it first." }
if ($visibility.Trim() -ne 'PUBLIC') {
    throw 'Expected a PUBLIC repository; refusing to publish to another destination'
}
$remote="https://github.com/$Repository.git"
if (-not (Test-Path -LiteralPath '.git')) {
    & git.exe init -b main
    if ($LASTEXITCODE -ne 0) { throw 'git init failed' }
}
$existing=(& git.exe remote get-url origin 2>$null)
if ($LASTEXITCODE -ne 0) {
    & git.exe remote add origin $remote
    if ($LASTEXITCODE -ne 0) { throw 'Cannot set origin' }
} elseif ($existing.Trim() -ne $remote) {
    throw "Wrong origin $existing. Expected $remote"
}
$branch=(& git.exe branch --show-current).Trim()
if ($branch -and $branch -ne 'main') { throw "Expected main branch; current branch: $branch" }
$version=(Get-Content -LiteralPath 'VERSION' -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid version' }
# Stage known source components, not a blanket 'git add --all'.
& git.exe add -- '.github' '.gitignore' '.env.example' 'README.md' 'VERSION' 'requirements.txt' `
    'Install-All.cmd' 'Install-All.ps1' 'Manage.cmd' 'Start-VM-Orchestrator.cmd' `
    'app' 'guest_agent' 'integrations' 'scripts' 'tools' 'docs'
if ($LASTEXITCODE -ne 0) { throw 'Staging source files failed' }
$files=@(& git.exe diff --cached --name-only --diff-filter=ACMR)
if ($LASTEXITCODE -ne 0) { throw 'Cannot review staged files' }
$disallowed = @($files | Where-Object {
    $_ -match '(^|/)(data|dist|\.git|\.venv|__pycache__|reports|tests)/' -or
    $_ -match '(^|/)\.env$' -or
    $_ -match '\.(sqlite3?|db|key|pem|pfx|p12|dpapi|crt|log|bak)$' -or
    $_ -match '(?i)(secret|credentials|session|token)\.(json|txt|yaml|yml)$'
})
if ($disallowed.Count -gt 0) { throw ("Sensitive paths staged: " + ($disallowed -join ', ')) }
# Reject known credential signatures in source contents, even for allowed paths.
# Build the signatures in parts so this scanner does not self-trigger.
$signatures = @(
    ('-----BEGIN ' + '(?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    ('g' + 'hp_[A-Za-z0-9]{30,}'),
    ('github' + '_pat_[A-Za-z0-9_]{30,}'),
    ('sk-' + '[A-Za-z0-9]{24,}'),
    ('xoxb-' + '[0-9]+-[A-Za-z0-9-]+')
)
foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { continue }
    $content=[IO.File]::ReadAllText((Join-Path $root $file))
    foreach ($signature in $signatures) {
        if ($content -match $signature) {
            throw "Possible embedded credential in $file. Stop and audit before public upload."
        }
    }
}
& git.exe diff --cached --check
if ($LASTEXITCODE -ne 0) { throw 'Whitespace or conflict marker check failed' }
Write-Host 'PUBLIC repository:' $Repository -ForegroundColor Yellow
Write-Host 'Staged file count:' $files.Count
Write-Host 'Review carefully: source code and internal defaults will become publicly readable.' -ForegroundColor Yellow
if (-not $PublicApproved) {
    $answer=Read-Host 'Type PUBLISH to confirm this PUBLIC push'
    if ($answer -cne 'PUBLISH') { throw 'Cancelled; no GitHub changes made' }
}
& git.exe diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    & git.exe commit -m "VMO v${version}: audited public release and updater fixes"
    if ($LASTEXITCODE -ne 0) { throw 'Commit failed; configure git user.name and git user.email' }
}
& git.exe push -u origin main
if ($LASTEXITCODE -ne 0) { throw 'Push failed. Check credentials or remote main history.' }
if (-not $SkipReleaseTag) {
    $tag="v$version"
    $localTag=@(& git.exe tag --list $tag)
    if ($localTag.Count -gt 0) { throw "Local tag already exists: $tag" }
    & git.exe ls-remote --exit-code --tags origin "refs/tags/$tag" *> $null
    if ($LASTEXITCODE -eq 0) { throw "Remote release tag already exists: $tag" }
    & git.exe tag $tag
    if ($LASTEXITCODE -ne 0) { throw 'Cannot tag release' }
    & git.exe push origin $tag
    if ($LASTEXITCODE -ne 0) { throw 'Tag push failed' }
}
Write-Host "Repository: https://github.com/$Repository" -ForegroundColor Green
Write-Host "Releases: https://github.com/$Repository/releases" -ForegroundColor Green
Write-Host 'GitHub Actions should build the ZIP and SHA256 after the release tag push.'
