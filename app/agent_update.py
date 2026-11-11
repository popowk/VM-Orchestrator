"""Administrator initiated Guest Agent upgrade from the already-enrolled WSS channel.

The host publishes only a ZIP built from its own reviewed Guest Agent source.
The Guest downloads via its pinned HTTPS CA and existing per-VM identity.
A *separate* SYSTEM process runs the existing rollback-capable installer, after
its WSS worker has had time to acknowledge dispatch. Never overwrite the live
Agent process from inside the command handler.
"""
from __future__ import annotations
import hashlib
import io
import json
import re
import sqlite3
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

RELEASE = json.loads((Path(__file__).resolve().parent.parent / 'guest_agent' / 'agent-release.json').read_text(encoding='utf-8'))['version']
HEX_ID = re.compile(r'^[0-9a-f]{32}$')
STAGING_DIR = r'C:\Program Files\VM-Orchestrator\GuestAgent\state\agent-updates'
CONF_PATH = r'C:\Program Files\VM-Orchestrator\GuestAgent\state\agent.json'


def source_files(source: Path) -> list[str]:
    installer = (source / 'Install-GuestAgentV2.ps1').read_text(encoding='utf-8-sig')
    # The actual installer defines the required inventory. Never copy a state/
    # directory, enrollment credentials, a user ZIP, or dynamically named files.
    matched = re.search(r'\$files\s*=\s*@\((.*?)\)', installer, re.S)
    if not matched:
        raise ValueError('Guest installer source inventory missing')
    names = re.findall(r"'([^']+)'", matched.group(1))
    if not names or len(names) > 80 or len(names) != len(set(names)):
        raise ValueError('Unsafe Guest inventory')
    files = sorted(set(names + ['Install-GuestAgentV2.ps1', 'Install-GuestAgentV2.cmd']))
    for name in files:
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,90}', name)
                or not (name.lower().endswith(('.py', '.ps1', '.cmd')) or name == 'agent-release.json')
                or not (source / name).is_file() or (source / name).is_symlink()):
            raise ValueError('Unsafe Guest source package')
    return files


def build_archive(source: Path) -> bytes:
    files = source_files(source)
    source_version = json.loads((source / 'agent-release.json').read_text(encoding='utf-8'))['version']
    if source_version != RELEASE or 'agent-release.json' not in files:
        raise ValueError('Guest release manifest/version mismatch')
    total = sum((source / f).stat().st_size for f in files)
    if total > 12 * 1024 * 1024:
        raise ValueError('Agent source exceeds controlled distribution limit')
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for name in files:
            z.write(source / name, 'guest_agent/' + name)
        runner = source.parent / 'scripts' / 'guest' / 'Run-GuestAgent-Upgrade.ps1'
        if not runner.is_file() or runner.is_symlink():
            raise ValueError('Upgrade runner missing')
        z.write(runner, 'Run-GuestAgent-Upgrade.ps1')
        z.writestr('release.json', json.dumps({'version': RELEASE,'files':files}))
    return memory.getvalue()


def newer_release(current_version: str, target_version: str = RELEASE) -> bool:
    """Only upgrade known, comparable V2 releases; never downgrade or reinstall."""
    pattern = r'^2\.[0-9]{1,5}\.[0-9]{1,5}$'
    if not isinstance(current_version, str) or not re.fullmatch(pattern, current_version):
        return False
    if not re.fullmatch(pattern, target_version):
        return False
    return tuple(map(int, current_version.split('.'))) < tuple(map(int, target_version.split('.')))


def build_bootstrap(identifier: str, expected_sha: str, target: str = RELEASE) -> str:
    if not HEX_ID.fullmatch(identifier) or not re.fullmatch('[0-9a-f]{64}', expected_sha) or target != RELEASE:
        raise ValueError('Invalid Agent update request')
    # No secrets or credentials are embedded in the queued command. The
    # SYSTEM-only state/agent.json is opened directly by the Guest Python process.
    # Python's urllib uses the Guest's private pinned CA, not -SkipCertificateCheck.
    code = ('import sys,json,ssl,urllib.request,shutil;'
            'c=json.load(open(sys.argv[1],encoding="utf-8-sig"));'
            'r=urllib.request.Request(sys.argv[2],headers={"X-Agent-ID":c["agent_id"],"X-Agent-Key":c["agent_key"]});'
            's=urllib.request.urlopen(r,context=ssl.create_default_context(cafile=c["ca_bundle"]),timeout=90);'
            'f=open(sys.argv[3],"wb");shutil.copyfileobj(s,f);f.close();s.close()')
    # Script is a constant template; all interpolated inputs are validated hex
    # strings from our own server-generated package.
    cmd = (
        "$ErrorActionPreference='Stop';"
        f"$c='{CONF_PATH}';"
        f"$d=Join-Path '{STAGING_DIR}' '{identifier}';"
        "New-Item -Path $d -ItemType Directory -Force|Out-Null;"
        "$z=Join-Path $d 'payload.zip';"
        "$v=Get-CimInstance Win32_Service -Filter \"Name='VMO-GuestAgentV2'\";"
        "$p=[regex]::Match($v.PathName,'^\"([^\"]+python(?:w)?\\.exe)\"').Groups[1].Value;"
        "if(-not $p){throw 'Agent Python path unavailable'};"
        f"$code='{code}';"
        f"$cfg=(Get-Content -Raw $c|ConvertFrom-Json);$u=$cfg.server_url.TrimEnd('/')+'/agent/agent-updates/{identifier}';"
        "$down=Join-Path $d 'download.py';"
        "Set-Content -LiteralPath $down -Value $code -Encoding UTF8;"
        "& $p $down $c $u $z;"
        "if($LASTEXITCODE -ne 0){throw 'Pinned HTTPS download failed'};"
        f"if((Get-FileHash -LiteralPath $z -Algorithm SHA256).Hash.ToLowerInvariant() -ne '{expected_sha}'){{throw 'Agent update SHA256 mismatch'}};"
        "$e=Join-Path $d 'payload';Expand-Archive -LiteralPath $z -DestinationPath $e -Force;"
        "$run=Join-Path $e 'Run-GuestAgent-Upgrade.ps1';"
        "if(-not (Test-Path -LiteralPath $run)){throw 'Upgrade runner missing'};"
        f"$launcherArgs='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"'+$run+'\" -Stage \"'+$e+'\" -Version {target}';"
        "Start-Process -FilePath 'powershell.exe' -ArgumentList $launcherArgs -WindowStyle Hidden;"
        "Write-Output 'UPGRADE_SCHEDULED_VERIFY_NEW_WSS_VERSION'"
    )
    if len(cmd) > 2000:
        raise ValueError('Bootstrap exceeds old Agent remote command limit')
    return cmd


class AgentUpdateManager:
    version = RELEASE
    def __init__(self, db_path: Path, source: Path):
        self.db_path = Path(db_path)
        self.source = Path(source)
        self.root = self.db_path.parent / 'agent-update-packages'
        self.root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as d:
            d.execute('''CREATE TABLE IF NOT EXISTS agent_upgrade_requests (
                 id TEXT PRIMARY KEY, vm_id TEXT NOT NULL, version TEXT NOT NULL,
                 sha256 TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                 operation_id TEXT, source TEXT NOT NULL DEFAULT 'manual')''')
            if 'source' not in {row[1] for row in d.execute('PRAGMA table_info(agent_upgrade_requests)')}:
                d.execute("ALTER TABLE agent_upgrade_requests ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
            d.execute('CREATE INDEX IF NOT EXISTS agent_upgrade_by_vm ON agent_upgrade_requests(vm_id,created_at)')

    def _db(self):
        d = sqlite3.connect(self.db_path,timeout=15)
        d.row_factory = sqlite3.Row
        return d

    def prepare(self, vm_id: str, source: str = 'manual') -> dict:
        if source not in ('manual', 'auto'):
            raise ValueError('Unknown upgrade source')
        identifier = uuid.uuid4().hex
        package = build_archive(self.source)
        digest = hashlib.sha256(package).hexdigest()
        path = self.root / (identifier + '.zip')
        path.write_bytes(package)
        stamp = datetime.now(timezone.utc)
        try:
            with self._db() as d:
                d.execute('''INSERT INTO agent_upgrade_requests
                      (id,vm_id,version,sha256,created_at,expires_at,operation_id,source)
                      VALUES(?,?,?,?,?,?,NULL,?)''',
                          (identifier,vm_id,RELEASE,digest,stamp.isoformat(),
                           (stamp+timedelta(hours=2)).isoformat(),source))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {'id':identifier,'version':RELEASE,'sha256':digest,'bytes':len(package)}

    def discard(self, identifier: str):
        """Clean a not-yet-queued update if dispatch fails."""
        with self._db() as d:
            row = d.execute('SELECT operation_id FROM agent_upgrade_requests WHERE id=?',
                            (identifier,)).fetchone()
            if row and row['operation_id'] is None:
                d.execute('DELETE FROM agent_upgrade_requests WHERE id=?', (identifier,))
                try:
                    (self.root / (identifier + '.zip')).unlink(missing_ok=True)
                except OSError:
                    pass

    def auto_retry_due(self, vm_id: str) -> bool:
        """No restart loops: minimum 90 minutes apart; maximum 3 auto tries/day.

        Manual requests are also honored to avoid competing with a human repair.
        """
        now = datetime.now(timezone.utc)
        with self._db() as d:
            recent = d.execute('''SELECT created_at,source FROM agent_upgrade_requests
                WHERE vm_id=? AND version=? ORDER BY created_at DESC LIMIT 1''',
                (vm_id,RELEASE)).fetchone()
            attempts = d.execute('''SELECT COUNT(*) FROM agent_upgrade_requests
                WHERE vm_id=? AND version=? AND source='auto' AND created_at>=?''',
                (vm_id,RELEASE,(now-timedelta(days=1)).isoformat())).fetchone()[0]
        if attempts >= 3:
            return False
        if recent:
            try:
                if now-datetime.fromisoformat(recent['created_at']) < timedelta(minutes=90):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def attach(self, identifier: str, operation_id: str):
        with self._db() as d:
            row=d.execute('UPDATE agent_upgrade_requests SET operation_id=? WHERE id=? AND operation_id IS NULL',
                          (operation_id,identifier))
            if row.rowcount != 1:raise ValueError('Agent upgrade package not available')

    def package_for_vm(self, vm_id: str, identifier: str) -> Path:
        if not HEX_ID.fullmatch(identifier):raise ValueError('Invalid upgrade package ID')
        with self._db() as d:
            r=d.execute('SELECT sha256,expires_at,operation_id FROM agent_upgrade_requests WHERE id=? AND vm_id=?',
                        (identifier,vm_id)).fetchone()
        if (not r or datetime.fromisoformat(r['expires_at']) < datetime.now(timezone.utc)):
            raise FileNotFoundError('Upgrade package expired or not assigned to this Guest')
        p=self.root / (identifier+'.zip')
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=r['sha256']:
            raise FileNotFoundError('Upgrade package missing or corrupted')
        return p

    def latest(self, vm_id: str):
        with self._db() as d:
            row=d.execute('''SELECT a.*,o.state AS status,o.result_json AS result
                FROM agent_upgrade_requests a LEFT JOIN remote_guest_ops o ON a.operation_id=o.id
                WHERE a.vm_id=? ORDER BY a.created_at DESC LIMIT 1''',(vm_id,)).fetchone()
        if not row:return None
        return {k:row[k] for k in ('id','version','created_at','operation_id','status','source')}

    def recent_pending(self, vm_id: str):
        latest=self.latest(vm_id)
        if not latest or not latest['operation_id']:return False
        try:
            created=datetime.fromisoformat(latest['created_at'])
            return datetime.now(timezone.utc)-created < timedelta(minutes=15)
        except ValueError:return True
