"""Create the ZIP + sha256 sidecar expected by the privileged self-updater.

Run in a clean checkout. Never package data, local secrets, git objects, or tests.
"""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
if not __import__('re').fullmatch(r'\d+\.\d+\.\d+', VERSION):
    sys.exit('Invalid VERSION')
DEST = ROOT / 'dist'
DEST.mkdir(exist_ok=True)
NAME = f'VM-Orchestrator-v{VERSION}.zip'
EXCLUDES = {'.git', '.github', 'tools', 'tests', 'data', 'dist', '__pycache__', '.venv', 'reports'}
SECRETS = {'.env'}

def excluded(rel: Path):
    return (any(x in EXCLUDES for x in rel.parts) or
            rel.name in SECRETS or rel.name.startswith('backup-before-') or
            rel.suffix.lower() in {'.pyc', '.pfx', '.key', '.pem', '.sqlite3', '.db', '.log'} or
            '.sqlite3-' in rel.name)

# Use git's committed inventory when running in a repository to avoid accidental untracked secrets.
try:
    listed = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z'], stderr=subprocess.DEVNULL)
    candidates = [ROOT / os.fsdecode(p) for p in listed.split(b'\0') if p]
except (OSError, subprocess.CalledProcessError):
    candidates = list(ROOT.rglob('*'))
files = sorted((p for p in candidates if p.is_file() and not excluded(p.relative_to(ROOT))), key=str)
for rel in ('VERSION', 'app/main.py', 'Start-VM-Orchestrator.cmd',
            'scripts/host/Apply-GitHubUpdate.ps1', 'Install-All.ps1'):
    if ROOT / rel not in files:
        raise SystemExit('Missing critical release file: ' + rel)
archive = DEST / NAME
with ZipFile(archive, 'w', ZIP_DEFLATED, compresslevel=9) as z:
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        z.write(path, f'VM-Orchestrator-v{VERSION}/{rel}')
    # Keep a reproducible inventory next to the installed release. Runtime
    # cleanup can distinguish extra/modified files from the exact shipped set.
    inventory = {path.relative_to(ROOT).as_posix(): {
        'size': path.stat().st_size,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()
    } for path in files}
    z.writestr(f'VM-Orchestrator-v{VERSION}/release-manifest.json',
               json.dumps({'version': VERSION, 'files': inventory},
                          ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8'))
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(DEST / (NAME + '.sha256')).write_text(f'{digest}  {NAME}\n', encoding='ascii')
print('Created:', archive)
print('SHA-256:', digest)
print('Included files:', len(files))
