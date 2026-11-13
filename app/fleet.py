"""Guest enrollment, package catalog and queued software updates.

Guest credentials are unrelated to the admin key. Only package update tasks (not
arbitrary commands) can be dispatched over the guest-facing API.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$')
SMBIOS_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',re.I)


def now(): return datetime.now(timezone.utc).isoformat()
def digest(s: str) -> str: return hashlib.sha256(s.encode('utf-8')).hexdigest()


class FleetError(ValueError):
    pass


class FleetStore:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as d:
            d.executescript('''
            CREATE TABLE IF NOT EXISTS fleet_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS vm_reinstalls (
              job_id TEXT PRIMARY KEY, vm_id TEXT NOT NULL, old_template_id TEXT NOT NULL,
              new_template_id TEXT NOT NULL, backup_path TEXT, status TEXT NOT NULL,
              created_at TEXT NOT NULL, completed_at TEXT);
            CREATE TABLE IF NOT EXISTS guest_enrollment (
              vm_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL UNIQUE, secret_hash TEXT NOT NULL,
              registered_at TEXT NOT NULL, last_seen TEXT, reported_json TEXT NOT NULL DEFAULT '{}',
              revoked_at TEXT);
            CREATE TABLE IF NOT EXISTS guest_enrollment_tokens (
              vm_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
              expires_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS update_packages (
              id TEXT PRIMARY KEY, product TEXT NOT NULL, version TEXT NOT NULL,
              filename TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
              uploaded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS guest_tasks (
              id TEXT PRIMARY KEY, vm_id TEXT NOT NULL, package_id TEXT NOT NULL,
              status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT,
              kind TEXT NOT NULL DEFAULT 'UPDATE', reference_id TEXT);
            CREATE INDEX IF NOT EXISTS guest_tasks_vm ON guest_tasks(vm_id,status);
            ''')
            # Idempotent migrations for upgraded v0.9 previews.
            cols={x[1] for x in d.execute('PRAGMA table_info(guest_tasks)')}
            if 'kind' not in cols: d.execute("ALTER TABLE guest_tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'UPDATE'")
            if 'reference_id' not in cols: d.execute('ALTER TABLE guest_tasks ADD COLUMN reference_id TEXT')
            if 'phase' not in cols: d.execute("ALTER TABLE guest_tasks ADD COLUMN phase TEXT NOT NULL DEFAULT ''")
            if 'options_json' not in cols: d.execute("ALTER TABLE guest_tasks ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}'")
            if 'progress_at' not in cols: d.execute('ALTER TABLE guest_tasks ADD COLUMN progress_at TEXT')
            enrollment_columns={r[1] for r in d.execute('PRAGMA table_info(guest_enrollment)')}
            if 'smbios_uuid' not in enrollment_columns:
                d.execute('ALTER TABLE guest_enrollment ADD COLUMN smbios_uuid TEXT')
            reinstall_columns={r[1] for r in d.execute('PRAGMA table_info(vm_reinstalls)')}
            if 'backup_deleted_at' not in reinstall_columns:
                d.execute('ALTER TABLE vm_reinstalls ADD COLUMN backup_deleted_at TEXT')
            package_columns={r[1] for r in d.execute('PRAGMA table_info(update_packages)')}
            if 'deleted_at' not in package_columns:
                d.execute('ALTER TABLE update_packages ADD COLUMN deleted_at TEXT')

    @contextmanager
    def db(self):
        d = sqlite3.connect(str(self.path), timeout=20)
        d.row_factory = sqlite3.Row
        try:
            d.execute('PRAGMA busy_timeout=20000')
            with d:
                yield d
        finally:
            d.close()

    def default_template(self):
        with self.db() as d:
            r = d.execute("SELECT value FROM fleet_settings WHERE key='default_template'").fetchone()
            return r['value'] if r else None

    def set_default_template(self, template_id: str):
        with self.db() as d:
            t = d.execute("SELECT status FROM templates WHERE id=?", (template_id,)).fetchone()
            if not t or t['status'] != 'SEALED_ATTESTED':
                raise FleetError('Choose a sealed golden template')
            d.execute("INSERT INTO fleet_settings VALUES('default_template',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (template_id,))
        return template_id

    def create_enrollment(self, vm_id: str, lifetime_minutes: int = 60) -> str:
        token = secrets.token_urlsafe(40)
        with self.db() as d:
            v = d.execute('SELECT state FROM vms WHERE id=?', (vm_id,)).fetchone()
            if not v or v['state'] != 'READY':
                raise FleetError('VM must be READY before guest enrollment')
            if d.execute('SELECT vm_id FROM guest_enrollment WHERE vm_id=? AND revoked_at IS NULL', (vm_id,)).fetchone():
                raise FleetError('Guest already registered; revoke it explicitly before re-enrollment')
            expiry = (datetime.now(timezone.utc) + timedelta(minutes=lifetime_minutes)).isoformat()
            d.execute('INSERT INTO guest_enrollment_tokens VALUES(?,?,?) ON CONFLICT(vm_id) DO UPDATE SET token_hash=excluded.token_hash, expires_at=excluded.expires_at', (vm_id, digest(token), expiry))
        return token

    def enroll(self, vm_id: str, token: str):
        if not token or len(token) > 200: raise FleetError('Invalid enrollment token')
        key = secrets.token_urlsafe(40)
        agent_id = str(uuid.uuid4())
        with self.db() as d:
            row = d.execute('SELECT token_hash,expires_at FROM guest_enrollment_tokens WHERE vm_id=?', (vm_id,)).fetchone()
            vm = d.execute('SELECT state FROM vms WHERE id=?', (vm_id,)).fetchone()
            if not vm or vm['state'] != 'READY' or not row or not hmac.compare_digest(row['token_hash'], digest(token)):
                raise FleetError('Enrollment rejected')
            if datetime.fromisoformat(row['expires_at']) <= datetime.now(timezone.utc):
                raise FleetError('Enrollment token expired')
            active = d.execute('SELECT revoked_at FROM guest_enrollment WHERE vm_id=?', (vm_id,)).fetchone()
            if active and active['revoked_at'] is None:
                raise FleetError('An active Guest Agent is already registered')
            d.execute('DELETE FROM guest_enrollment_tokens WHERE vm_id=?', (vm_id,))
            d.execute('INSERT INTO guest_enrollment (vm_id,agent_id,secret_hash,registered_at,reported_json,revoked_at) VALUES(?,?,?,?,?,NULL) '
                      'ON CONFLICT(vm_id) DO UPDATE SET agent_id=excluded.agent_id,secret_hash=excluded.secret_hash,registered_at=excluded.registered_at,last_seen=NULL,reported_json=excluded.reported_json,revoked_at=NULL,smbios_uuid=NULL',
                      (vm_id, agent_id, digest(key), now(), '{}'))
        return {'vm_id':vm_id, 'agent_id':agent_id, 'agent_key':key}

    def authorized_vm(self, agent_id: str, secret: str):
        with self.db() as d:
            r = d.execute('SELECT vm_id,secret_hash,revoked_at FROM guest_enrollment WHERE agent_id=?', (agent_id,)).fetchone()
            if not r or r['revoked_at'] or not secret or not hmac.compare_digest(r['secret_hash'], digest(secret)):
                raise FleetError('Agent unauthorized')
            vm = d.execute('SELECT state FROM vms WHERE id=?', (r['vm_id'],)).fetchone()
            if not vm or vm['state'] != 'READY':
                raise FleetError('VM is not active')
            return r['vm_id']

    def revoke_vm(self, vm_id: str):
        with self.db() as d:
            d.execute('UPDATE guest_enrollment SET revoked_at=? WHERE vm_id=? AND revoked_at IS NULL', (now(),vm_id))
            d.execute('DELETE FROM guest_enrollment_tokens WHERE vm_id=?', (vm_id,))
            d.execute("UPDATE guest_tasks SET status='CANCELLED',message='Guest identity revoked',completed_at=? WHERE vm_id=? AND status IN ('QUEUED','CLAIMED','RUNNING')", (now(),vm_id))

    def heartbeat(self, vm_id: str, report: dict):
        if not isinstance(report,dict) or len(json.dumps(report)) > 16384:
            raise FleetError('Report too large or invalid')
        smbios=report.get('smbios_uuid')
        if smbios is not None:
            if not isinstance(smbios,str) or not SMBIOS_UUID.fullmatch(smbios) or smbios.lower() in (
                '00000000-0000-0000-0000-000000000000',
                'ffffffff-ffff-ffff-ffff-ffffffffffff'):
                raise FleetError('Invalid SMBIOS UUID reported')
            smbios=smbios.lower()
        with self.db() as d:
            existing=d.execute('SELECT smbios_uuid,revoked_at FROM guest_enrollment WHERE vm_id=?',(vm_id,)).fetchone()
            if not existing or existing['revoked_at'] is not None:
                raise FleetError('Agent registration not active')
            if smbios:
                if existing['smbios_uuid'] and existing['smbios_uuid']!=smbios:
                    raise FleetError('Guest SMBIOS UUID differs from registered VM identity')
                duplicate=d.execute('SELECT vm_id FROM guest_enrollment WHERE smbios_uuid=? '
                    'AND vm_id<>? AND revoked_at IS NULL LIMIT 1',(smbios,vm_id)).fetchone()
                if duplicate:raise FleetError('Guest SMBIOS UUID already belongs to another VM')
                d.execute('UPDATE guest_enrollment SET smbios_uuid=COALESCE(smbios_uuid,?) WHERE vm_id=?',
                          (smbios,vm_id))
            d.execute('UPDATE guest_enrollment SET last_seen=?, reported_json=? WHERE vm_id=?',
                      (now(),json.dumps(report, ensure_ascii=False),vm_id))
        self.reconcile_auto_deploy(vm_id)

    def guests(self):
        with self.db() as d:
            return [{**dict(r), 'reported':json.loads(r['reported_json'] or '{}')} for r in d.execute(
                'SELECT vm_id,agent_id,registered_at,last_seen,reported_json,revoked_at FROM guest_enrollment ORDER BY registered_at DESC')]

    def add_package(self, product: str, version: str, sha256: str, size: int, name: str, package_id: str | None = None):
        if not re.fullmatch(r'portable-p-[0-9a-f]{20}', product) or not VERSION.fullmatch(version):
            raise FleetError('Unsupported product or unsafe version')
        package_id = package_id or str(uuid.uuid4())
        with self.db() as d:
            d.execute('INSERT INTO update_packages(id,product,version,filename,sha256,bytes,uploaded_at) VALUES(?,?,?,?,?,?,?)',
                      (package_id,product,version,name,sha256,size,now()))
        return package_id

    def package(self, package_id: str):
        with self.db() as d:
            r = d.execute('SELECT * FROM update_packages WHERE id=? AND deleted_at IS NULL', (package_id,)).fetchone()
            return dict(r) if r else None

    def packages(self):
        with self.db() as d:
            return [dict(r) for r in d.execute("SELECT * FROM update_packages WHERE filename != 'approved-plugin-command' AND deleted_at IS NULL ORDER BY uploaded_at DESC")]

    def delete_package(self, package_id: str):
        """Remove an approved ZIP but retain immutable update-task audit history.

        Refuse deletion if this artifact is pinned to a customer self-service
        action or participates in an unfinished update. Move bytes out of the
        serving directory before committing the tombstone. This also prevents
        old completed tasks from being replayed or rolled back after deletion.
        """
        try:
            parsed=uuid.UUID(package_id)
            if str(parsed)!=package_id:raise ValueError()
        except (ValueError,TypeError):
            raise FleetError('Invalid update package ID')
        root=self.path.parent/'approved_packages'
        source=root/(package_id+'.zip')
        quarantined=None
        with self.db() as d:
            d.execute('BEGIN IMMEDIATE')
            row=d.execute('SELECT id,filename FROM update_packages WHERE id=? AND deleted_at IS NULL',
                (package_id,)).fetchone()
            if not row or row['filename']=='approved-plugin-command':
                raise FleetError('Update package does not exist')
            if d.execute("SELECT 1 FROM guest_tasks WHERE package_id=? AND status IN ('QUEUED','CLAIMED','RUNNING','VERIFY_REQUIRED','UNKNOWN','TIMED_OUT') LIMIT 1",
                         (package_id,)).fetchone():
                raise FleetError('更新包仍有正在执行的任务，请先等待完成')
            if d.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_self_actions'").fetchone():
                if d.execute('SELECT 1 FROM business_self_actions WHERE package_id=? LIMIT 1',(package_id,)).fetchone():
                    raise FleetError('更新包已绑定客户自助操作，请先修改或删除该操作')
            if source.is_symlink():raise FleetError('Unsafe package artifact')
            if source.exists():
                quarantine=root/'.deleted';quarantine.mkdir(parents=True,exist_ok=True)
                quarantined=quarantine/(package_id+'.'+uuid.uuid4().hex+'.zip')
                try:source.replace(quarantined)
                except OSError as ex:raise FleetError('无法移走更新包，文件可能正在被使用') from ex
            try:
                d.execute('UPDATE update_packages SET deleted_at=? WHERE id=? AND deleted_at IS NULL',(now(),package_id))
                d.execute("UPDATE portable_profiles SET auto_deploy=0,active_package_id='' WHERE active_package_id=?",
                          (package_id,))
                if d.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vmo_security_audit'").fetchone():
                    import time
                    d.execute('INSERT INTO vmo_security_audit(id,action,detail,at) VALUES(?,?,?,?)',
                        (str(uuid.uuid4()),'UPDATE_PACKAGE_DELETED',package_id,int(time.time())))
            except Exception:
                if quarantined and quarantined.exists():quarantined.replace(source)
                raise
        if quarantined:
            try:quarantined.unlink()
            except OSError:pass  # quarantined outside downloadable path; may be cleaned later
        return {'deleted':True,'package_id':package_id}

    def queue_update(self, vm_id: str, package_id: str, options: dict | None = None):
        with self.db() as d:
            d.execute('BEGIN IMMEDIATE')  # serialize with package revocation
            vm = d.execute('SELECT state FROM vms WHERE id=?', (vm_id,)).fetchone()
            agent = d.execute('SELECT revoked_at,reported_json,last_seen FROM guest_enrollment WHERE vm_id=?', (vm_id,)).fetchone()
            pack = d.execute('SELECT id,product FROM update_packages WHERE id=? AND deleted_at IS NULL', (package_id,)).fetchone()
            busy = d.execute("SELECT id FROM guest_tasks WHERE vm_id=? AND status IN ('QUEUED','CLAIMED','RUNNING')", (vm_id,)).fetchone()
            if not vm or vm['state'] != 'READY' or not agent or agent['revoked_at'] or not pack or busy:
                raise FleetError('VM not ready, no active agent, unknown package or previous update pending')
            active=d.execute('SELECT auto_deploy FROM portable_profiles WHERE active_package_id=?',
                             (package_id,)).fetchone()
            if not active:
                raise FleetError('该版本不是当前生效版本，请使用最新的软件 ZIP')
            is_auto=bool(options and options.get('auto_deploy'))
            if is_auto and not active['auto_deploy']:
                raise FleetError('自动推送已被管理员关闭')
            if is_auto and d.execute(
                    'SELECT 1 FROM guest_tasks WHERE vm_id=? AND package_id=? LIMIT 1',
                    (vm_id,package_id)).fetchone():
                raise FleetError('此版本已经向该机器下发过，自动模式不重复执行')

            # New updates are exclusively the server-reviewed portable ZIP catalog.
            # Retain historical task rows for auditing; do not dispatch old modes.
            portable=re.fullmatch(r'portable-(p-[0-9a-f]{20})',pack['product'])
            if not portable:
                raise FleetError('旧版安装适配器已停用，请上传统一便携程序 ZIP')
            try:
                report=json.loads(agent['reported_json'] or '{}')
            except (TypeError,ValueError):
                report={}
            if not isinstance(report,dict):
                report={}
            profile=d.execute(
                '''SELECT p.id,p.folder,p.launcher,
                    COALESCE(m.version_file,p.version_file) AS version_file,
                    COALESCE(m.version_source,p.version_source) AS version_source
                   FROM portable_profiles AS p
                   JOIN portable_package_meta AS m ON m.profile_id=p.id
                   WHERE p.id=? AND m.package_id=?''',
                (portable.group(1),package_id)).fetchone()
            if not profile:
                raise FleetError('更新包没有可用的软件目录配置，请重新上传')
            options={'mode':'portable_catalog','portable':dict(profile),'auto_deploy':is_auto}
            raw_version=report.get('guest_agent','')
            try:
                parts=tuple(int(x) for x in raw_version.split('.'))
            except (AttributeError,ValueError,TypeError):
                parts=()
            if len(parts)!=3 or parts<(2,0,27):
                raise FleetError('此客户机需要 Guest Agent v2.0.27 或更新版本')
            # The Guest inspects its actual disk at execution time:
            # missing C:\\dsh\\<folder> is a FIRST INSTALL, not an error.
            # Never rely on heartbeat inventory as installation authorization.
            options['portable']['deploy_mode']='install_or_update'
            if not agent['last_seen'] or (datetime.now(timezone.utc)-datetime.fromisoformat(agent['last_seen'])).total_seconds() > 600:
                raise FleetError('Guest Agent 心跳已超出 10 分钟；请先恢复自动心跳')
            id_ = str(uuid.uuid4())
            d.execute("INSERT INTO guest_tasks(id,vm_id,package_id,status,created_at,options_json) VALUES(?,?,?,'QUEUED',?,?)",
                      (id_,vm_id,package_id,now(),json.dumps(options or {},ensure_ascii=False)))
        return id_

    def reconcile_auto_deploy(self, vm_id: str | None = None) -> dict:
        """Best-effort, idempotent adoption of each opted-in active release.

        Called after heartbeat, new upload or toggling auto on. Terminal
        results are never retried automatically, even if they failed.
        """
        with self.db() as d:
            if not d.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='portable_profiles'").fetchone():
                return {'queued':[], 'skipped':[], 'policies':0}
            rows=d.execute('''SELECT p.id,p.active_package_id FROM portable_profiles p
                JOIN update_packages u ON u.id=p.active_package_id AND u.deleted_at IS NULL
                WHERE p.auto_deploy=1 AND p.active_package_id!='' ''').fetchall()
            vm_ids=([vm_id] if vm_id else [
                row[0] for row in d.execute('''SELECT e.vm_id FROM guest_enrollment e
                    JOIN vms v ON v.id=e.vm_id AND v.state='READY'
                    WHERE e.revoked_at IS NULL AND e.last_seen IS NOT NULL
                    ORDER BY e.last_seen DESC LIMIT 500''')])
        result={'queued':[], 'skipped':[], 'policies':len(rows)}
        for profile in rows:
            for target in vm_ids:
                try:
                    task=self.queue_update(target,profile['active_package_id'],
                                           options={'auto_deploy':True})
                    result['queued'].append({'vm_id':target,'task_id':task})
                except FleetError as exc:
                    result['skipped'].append({'vm_id':target,'reason':str(exc)[:160]})
        return result

    def tasks(self, vm_id: str | None = None, limit: int = 100, offset: int = 0):
        limit = max(1, min(int(limit), 100)); offset = max(0, int(offset))
        with self.db() as d:
            if vm_id: rows = d.execute('SELECT * FROM guest_tasks WHERE vm_id=? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?',(vm_id,limit,offset))
            else: rows = d.execute('SELECT * FROM guest_tasks ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?',(limit,offset))
            return [dict(r) for r in rows]

    def count_tasks(self, vm_id: str | None = None) -> int:
        with self.db() as d:
            if vm_id:
                row = d.execute('SELECT COUNT(*) FROM guest_tasks WHERE vm_id=?',(vm_id,)).fetchone()
            else:
                row = d.execute('SELECT COUNT(*) FROM guest_tasks').fetchone()
            return int(row[0])

    def claim(self, vm_id: str):
        # A previously delivered software task can have an unknown outcome.
        # Mark it for operator review; NEVER automatically run it again.
        from datetime import datetime, timezone, timedelta
        cutoff=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
        with self.db() as d:
            d.execute("UPDATE guest_tasks SET status='FAILED', message=?,completed_at=? "
                "WHERE vm_id=? AND status='CLAIMED' AND claimed_at<?",
                ('Task result unknown after Guest disconnect; inspect Guest before retrying',now(),vm_id,cutoff))
            # Do not retry a potentially destructive update after WSS loss. A
            # long-abandoned RUNNING task becomes review-required, not QUEUED.
            progress_cutoff=(datetime.now(timezone.utc)-timedelta(minutes=90)).isoformat()
            d.execute("UPDATE guest_tasks SET status='FAILED',message=?,completed_at=? "
                      "WHERE vm_id=? AND status='RUNNING' AND progress_at<?",
                      ('Update outcome unknown after lost heartbeat; review Guest update-journals before any retry',now(),vm_id,progress_cutoff))
            task = d.execute("SELECT * FROM guest_tasks WHERE vm_id=? AND status='QUEUED' ORDER BY created_at LIMIT 1",(vm_id,)).fetchone()
            # Old adapters were retired: fail only tasks that have NEVER been
            # claimed, rather than accidentally executing historical scripts.
            # Previously CLAIMED/RUNNING tasks retain their audit outcomes.
            while task:
                pkg=d.execute('SELECT * FROM update_packages WHERE id=?',(task['package_id'],)).fetchone()
                if (pkg and task['kind']=='UPDATE' and
                    re.fullmatch(r'portable-p-[0-9a-f]{20}',pkg['product'])):
                    policy=d.execute('SELECT active_package_id,auto_deploy FROM portable_profiles WHERE id=?',
                                     (pkg['product'][len('portable-'):],)).fetchone()
                    configuration=json.loads(task['options_json'] or '{}')
                    if (policy and policy['active_package_id']==pkg['id'] and
                            (not configuration.get('auto_deploy') or policy['auto_deploy'])):
                        break
                d.execute("UPDATE guest_tasks SET status='FAILED',message=?,completed_at=? WHERE id=? AND status='QUEUED'",
                          ('旧版更新方式已停用，任务未执行；请上传新 ZIP 更新包',now(),task['id']))
                task=d.execute("SELECT * FROM guest_tasks WHERE vm_id=? AND status='QUEUED' ORDER BY created_at LIMIT 1",(vm_id,)).fetchone()
            if not task:return None
            d.execute("UPDATE guest_tasks SET status='CLAIMED', claimed_at=? WHERE id=? AND status='QUEUED'",(now(),task['id']))
            return {'id':task['id'], 'product':pkg['product'], 'version':pkg['version'],
                    'package_id':pkg['id'], 'sha256':pkg['sha256'], 'size':pkg['bytes'],
                    'kind':task['kind'],'reference_id':task['reference_id'],
                    'options':json.loads(task['options_json'] or '{}')}

    def report_phase(self, vm_id: str, task_id: str, phase: str):
        """Authenticated Guest update progress, limited to fixed non-secret phases."""
        phases={'starting','downloaded','staged','stopped','swapped','healthy',
                'rolled_back_healthy','recovery_required','verified'}
        if phase not in phases: raise FleetError('Invalid update phase')
        with self.db() as d:
            cursor=d.execute("UPDATE guest_tasks SET status='RUNNING',phase=?,progress_at=? "
                             "WHERE vm_id=? AND id=? AND status IN ('CLAIMED','RUNNING')",
                             (phase,now(),vm_id,task_id))
            if cursor.rowcount!=1:raise FleetError('Update progress not assigned or already finished')

    def report_task(self, vm_id: str, task_id: str, ok: bool, message: str):
        with self.db() as d:
            row = d.execute('SELECT status,message FROM guest_tasks WHERE id=? AND vm_id=?',(task_id,vm_id)).fetchone()
            if not row:raise FleetError('Update task not assigned to this VM')
            if row['status'] in ('SUCCEEDED','FAILED','VERIFY_REQUIRED') and row['message']!='Task result unknown after Guest disconnect; inspect Guest before retrying':
                # Duplicate acknowledgement after a WSS reconnect.
                return
            if row['status'] not in ('CLAIMED','RUNNING') and row['message']!='Task result unknown after Guest disconnect; inspect Guest before retrying':
                raise FleetError('Update task is not claimed by this VM')
            completed='SUCCEEDED' if ok else 'FAILED'
            if ok:
                try:
                    data=json.loads(message)
                    if isinstance(data,dict) and data.get('runtime_version_api')=='not_verified':
                        completed='VERIFY_REQUIRED'
                    elif isinstance(data,dict) and data.get('version_evidence')=='command_exit_only':
                        completed='VERIFY_REQUIRED'
                except (ValueError,TypeError):pass
            d.execute('UPDATE guest_tasks SET status=?, message=?,completed_at=? WHERE id=? AND vm_id=?',
                      (completed,str(message)[:800],now(),task_id,vm_id))

    def reinstall_record(self, job_id: str, vm_id: str, old_id: str, new_id: str, status: str, backup: str = ''):
        with self.db() as d:
            if status=='QUEUED':
                d.execute('INSERT INTO vm_reinstalls '
                          '(job_id,vm_id,old_template_id,new_template_id,backup_path,status,created_at,completed_at) '
                          'VALUES(?,?,?,?,?,?,?,NULL)',
                          (job_id,vm_id,old_id,new_id,backup,status,now()))
            else:
                d.execute('UPDATE vm_reinstalls SET status=?,backup_path=?,completed_at=? WHERE job_id=?',
                          (status,backup,now(),job_id))

    def mark_reinstall_backup_deleted(self, job_id: str, backup_path: str) -> None:
        """Retain the reinstall audit row; mark its old backup permanently removed."""
        with self.db() as d:
            cur=d.execute("UPDATE vm_reinstalls SET backup_deleted_at=? "
                          "WHERE job_id=? AND backup_path=? AND status='SUCCEEDED' "
                          "AND backup_deleted_at IS NULL", (now(),job_id,backup_path))
            if cur.rowcount != 1:
                raise FleetError('Reinstall backup changed during cleanup; inspect history')

    def reinstalls(self, vm_id: str | None = None):
        with self.db() as d:
            rows = d.execute('SELECT * FROM vm_reinstalls WHERE vm_id=? ORDER BY created_at DESC',(vm_id,)) if vm_id else d.execute('SELECT * FROM vm_reinstalls ORDER BY created_at DESC LIMIT 100')
            return [dict(r) for r in rows]
