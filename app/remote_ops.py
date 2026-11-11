"""Authenticated, auditable Guest V2 remote operations.

Never expose this module's administrator routes through the Guest Gateway. The
shared SQLite database is the transport between Host Agent (127.0.0.1:8765)
and Guest Gateway (192.168.2.99:8766). Guest authentication is always via
FleetStore.authorized_vm. File transfers are staged and never executed.
"""
from __future__ import annotations
import base64
import hashlib
import os
import shutil
import ntpath
import ipaddress
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
from .app_vault import dpapi

UPLOAD_CHUNK = 1024*1024  # bounded memory, not bounded file length
DEFAULT_UPLOAD_DIR = "C:\\dsh\\"
MAX_RESULT = 64*1024
MAX_COMMAND_CMD = 8000  # cmd.exe command parser is limited to around 8191 characters
MAX_COMMAND_POWERSHELL = 12000  # keep EncodedCommand below the Win32 command-line limit
MAX_COMMAND_BYTES = 64*1024  # protect SQLite/WSS and allow UTF-8 / long scripts
SAFE_FILENAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}$')
APP_IDS = {'snowluma','dsh-fish-soul'}
ALLOWED_DIAGNOSTICS = {'hostname', 'network', 'processes', 'agent', 'software'}
ACTIONS = {'DIAGNOSTIC','COMMAND','UPLOAD','APP_HTTP','FILES_LIST','FILE_READ'}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class RemoteError(ValueError):
    pass


def is_safe_app_url(url: str) -> bool:
    """Proxy can never be used to access VM's network or metadata services."""
    try:
        p = urlparse(url)
        return p.scheme == 'http' and p.hostname in ('127.0.0.1', 'localhost') and p.port is not None \
            and 1 <= p.port <= 65535 and not p.username and not p.password \
            and not p.fragment and p.path in ('','/')
    except ValueError:
        return False


def validate_upload_filename(filename: str) -> str:
    if not isinstance(filename,str) or not 1<=len(filename)<=180 or len(filename.encode('utf-8'))>600:
        raise RemoteError('文件名为空或过长')
    if filename in ('.','..') or filename.rstrip(' .')!=filename or any(ord(c)<32 or c in '<>:"/\\|?*' for c in filename):
        raise RemoteError('文件名包含 Windows 不支持的字符')
    if re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)',filename):
        raise RemoteError('文件名为 Windows 保留设备名')
    return filename


def validate_command_directory(raw: str) -> str:
    """Admin-selected cwd; unlike upload destinations, system folders are valid."""
    if not isinstance(raw,str) or not 3<=len(raw)<=900:
        raise RemoteError('工作目录格式错误')
    raw=raw.replace('/','\\')
    if not re.match(r'^[A-Za-z]:\\',raw) or any(c in raw[3:] for c in '<>:"|?*\x00'):
        raise RemoteError('工作目录须为 Windows 本地绝对路径')
    if any(p in ('.','..') for p in raw[3:].split('\\')):
        raise RemoteError('工作目录不能包含相对路径片段')
    return ntpath.normpath(raw)


def validate_upload_directory(raw: str) -> str:
    """Require an explicit local Windows drive path; reject network, ADS and OS roots.

    Only the host administrator can choose the path. The Guest checks it again
    before writing. No environment expansion or arbitrary UNC shares.
    """
    if not isinstance(raw,str) or not 3<=len(raw)<=900:
        raise RemoteError('目标目录必须是 Windows 本地绝对路径')
    raw=raw.replace('/', '\\')
    if not re.match(r'^[A-Za-z]:\\',raw) or any(c in raw[3:] for c in '<>:"|?*\x00'):
        raise RemoteError('上传目录必须类似 C:\\dsh\\，不能使用 UNC 或特殊设备路径')
    segments=raw[3:].split('\\')
    if any(x in ('.','..') or x.rstrip(' .')!=x or re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)',x) for x in segments if x):
        raise RemoteError('目录包含 Windows 不允许或不安全的路径片段')
    clean=ntpath.normpath(raw)
    if len(clean)<=3 or re.match(r'(?i)^[a-z]:\\(?:windows|program files(?: \(x86\))?|programdata)(?:\\|$)',clean):
        raise RemoteError('不允许上传到磁盘根目录或系统目录')
    return clean


class RemoteStore:
    def __init__(self, db_path):
        self.path=Path(db_path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.files=self.path.parent/'remote_uploads'
        self.files.mkdir(parents=True,exist_ok=True)
        with self.db() as d:
            d.executescript('''
            CREATE TABLE IF NOT EXISTS remote_guest_ops (
              id TEXT PRIMARY KEY, vm_id TEXT NOT NULL, action TEXT NOT NULL,
              args_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'QUEUED',
              result_json TEXT, created_at TEXT NOT NULL,
              claimed_at TEXT, completed_at TEXT, expires_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS remote_app_secrets (
               vm_id TEXT NOT NULL, app_id TEXT NOT NULL, sealed_code BLOB NOT NULL,
               changed_at TEXT NOT NULL, PRIMARY KEY(vm_id,app_id));
            CREATE INDEX IF NOT EXISTS remote_ops_vm ON remote_guest_ops(vm_id,state,created_at);
            CREATE TABLE IF NOT EXISTS remote_guest_apps (
              vm_id TEXT NOT NULL, app_id TEXT NOT NULL, url TEXT NOT NULL,
              version TEXT, auth_state TEXT NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(vm_id,app_id));
            CREATE TABLE IF NOT EXISTS remote_app_launches (
              ticket_hash TEXT PRIMARY KEY, service_ref TEXT NOT NULL,
              vm_id TEXT NOT NULL, app_id TEXT NOT NULL, expires_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS remote_app_sessions (
              relay_id TEXT PRIMARY KEY, session_hash TEXT NOT NULL UNIQUE,
              service_ref TEXT NOT NULL, vm_id TEXT NOT NULL, app_id TEXT NOT NULL,
              expires_at INTEGER NOT NULL);
            -- A route identifier is stable for a (business service, VM, app).
            -- Authentication is NOT the route identifier: each launch rotates
            -- a secret and creates an independently expiring browser session.
            CREATE TABLE IF NOT EXISTS remote_app_routes (
              route_id TEXT PRIMARY KEY, service_ref TEXT NOT NULL,
              vm_id TEXT NOT NULL, app_id TEXT NOT NULL,
              UNIQUE(service_ref,vm_id,app_id));
            CREATE TABLE IF NOT EXISTS remote_app_route_sessions (
              route_id TEXT NOT NULL, session_hash TEXT PRIMARY KEY,
              expires_at INTEGER NOT NULL,
              FOREIGN KEY(route_id) REFERENCES remote_app_routes(route_id));
            CREATE INDEX IF NOT EXISTS idx_remote_app_route_sessions_expiry
              ON remote_app_route_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS remote_guest_links (
              vm_id TEXT PRIMARY KEY, connection_id TEXT, state TEXT NOT NULL,
              connected_at TEXT, last_wss_heartbeat TEXT, disconnected_at TEXT,
              disconnect_reason TEXT, last_client_error TEXT, last_client_error_at TEXT);
            CREATE TABLE IF NOT EXISTS remote_guest_link_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, vm_id TEXT NOT NULL,
              event TEXT NOT NULL, reason TEXT, recorded_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_remote_link_events_vm ON remote_guest_link_events(vm_id,id);
            ''')
            cols={row['name'] for row in d.execute('PRAGMA table_info(remote_guest_ops)')}
            if 'started_at' not in cols:
                d.execute('ALTER TABLE remote_guest_ops ADD COLUMN started_at TEXT')
            if 'attempt_count' not in cols:
                d.execute('ALTER TABLE remote_guest_ops ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0')
            if 'last_error' not in cols:
                d.execute('ALTER TABLE remote_guest_ops ADD COLUMN last_error TEXT')
            app_cols={row['name'] for row in d.execute('PRAGMA table_info(remote_guest_apps)')}
            if 'is_available' not in app_cols:d.execute('ALTER TABLE remote_guest_apps ADD COLUMN is_available INTEGER NOT NULL DEFAULT 0')
            if 'reported_at' not in app_cols:d.execute('ALTER TABLE remote_guest_apps ADD COLUMN reported_at TEXT')
            secret_cols={row['name'] for row in d.execute('PRAGMA table_info(remote_app_secrets)')}
            if 'source_reported_at' not in secret_cols:d.execute('ALTER TABLE remote_app_secrets ADD COLUMN source_reported_at TEXT')

    @contextmanager
    def db(self):
        d=sqlite3.connect(str(self.path),timeout=15)
        d.row_factory=sqlite3.Row
        try:
            d.execute('PRAGMA busy_timeout=15000')
            with d: yield d
        finally: d.close()

    def queue(self, vm_id: str, action: str, params: dict, *, ttl=300):
        if action not in ACTIONS: raise RemoteError('Unsupported operation')
        if not isinstance(params,dict):
            raise RemoteError('Invalid operation parameters')
        encoded=json.dumps(params, ensure_ascii=(action!='COMMAND'), separators=(',',':'))
        if len(encoded.encode('utf-8')) > (MAX_COMMAND_BYTES if action=='COMMAND' else 9000):
            raise RemoteError('Operation parameters exceed transport limit')
        if action=='DIAGNOSTIC' and params.get('name') not in ALLOWED_DIAGNOSTICS:
            raise RemoteError('Unapproved diagnostic')
        if action=='COMMAND':
            if params.get('shell') not in ('powershell','cmd') or not isinstance(params.get('command'),str) \
               or not 1<=len(params['command'])<=(MAX_COMMAND_CMD if params['shell']=='cmd' else MAX_COMMAND_POWERSHELL) or not params.get('confirmed') or params.get('privilege')!='system' \
               or params.get('run_mode','wait') not in ('wait','detached','interactive') \
               or not isinstance(params.get('timeout_seconds',60),int) or not 5<=params.get('timeout_seconds',60)<=300:
                raise RemoteError('Command requires admin confirmation, or exceeds CMD 8000 / PowerShell 12000 characters')
            if params.get('cwd') is not None and params.get('cwd') != '':
                validate_command_directory(params['cwd'])
        if action=='UPLOAD':
            if len(params.get('sha256',''))!=64 or not isinstance(params.get('bytes'),int) or params['bytes']<0:
                raise RemoteError('Unsafe upload metadata')
            validate_upload_filename(params.get('filename',''))
            validate_upload_directory(params.get('directory',DEFAULT_UPLOAD_DIR))
        if action in ('FILES_LIST','FILE_READ'):
            if params.get('area') not in ('logs','incoming'):
                raise RemoteError('Only Guest Agent logs and upload staging directories may be inspected')
            if action=='FILE_READ' and (params['area']!='logs' or not SAFE_FILENAME.fullmatch(params.get('filename',''))):
                raise RemoteError('Only a safe log filename may be read')
        if action=='APP_HTTP':
            if params.get('app_id') not in APP_IDS or params.get('method') not in ('GET','POST'):
                raise RemoteError('Invalid application proxy operation')
            if not isinstance(params.get('path'),str) or not params['path'].startswith('/') or params['path'].startswith('//'):
                raise RemoteError('Unsafe proxy path')
        op_id=str(uuid.uuid4())
        exp=(datetime.now(timezone.utc)+timedelta(seconds=ttl)).isoformat()
        with self.db() as d:
            d.execute('INSERT INTO remote_guest_ops(id,vm_id,action,args_json,created_at,expires_at) VALUES(?,?,?,?,?,?)',
                      (op_id,vm_id,action,json.dumps(params,separators=(',',':')),utcnow(),exp))
        return op_id

    def recover_stale(self, vm_id: str | None = None):
        """Recover only idempotent diagnostics. Never replay commands or uploads.

        A WSS send is not evidence of execution. If no started/result arrived,
        a lost diagnostic may be retried up to three total deliveries.
        Other operations become TIMED_OUT with unknown outcome, not FAILED.
        """
        now_dt=datetime.now(timezone.utc)
        claim_limit=(now_dt-timedelta(seconds=45)).isoformat()
        run_limit=(now_dt-timedelta(seconds=360)).isoformat()
        # Large file transfer is a streaming operation; allow slow links up to
        # one day without redispatching an already-started upload.
        upload_run_limit=(now_dt-timedelta(hours=24)).isoformat()
        stamp=now_dt.isoformat()
        with self.db() as d:
            scope=' AND vm_id=?' if vm_id else ''
            vm_params=(vm_id,) if vm_id else ()
            d.execute("UPDATE remote_guest_ops SET state='EXPIRED', completed_at=?, "
                      "last_error='Queue TTL elapsed before delivery' "
                      "WHERE state='QUEUED' AND expires_at<?" + scope,
                      (stamp,stamp,*vm_params))
            candidates=d.execute("SELECT id,action,state,attempt_count FROM remote_guest_ops "
                "WHERE state IN ('CLAIMED','RUNNING') "
                "AND ((state='CLAIMED' AND claimed_at<?) OR (state='RUNNING' AND "
                "COALESCE(started_at,claimed_at) < CASE WHEN action='UPLOAD' THEN ? ELSE ? END))" + scope,
                (claim_limit,upload_run_limit,run_limit,*vm_params)).fetchall()
            for row in candidates:
                if row['state']=='CLAIMED' and row['action'] in ('DIAGNOSTIC','FILES_LIST','FILE_READ') and row['attempt_count']<3:
                    d.execute("UPDATE remote_guest_ops SET state='QUEUED', claimed_at=NULL, "
                        "started_at=NULL,last_error='Diagnostic delivery timed out; safe retry' "
                        "WHERE id=? AND state=?",(row['id'],row['state']))
                else:
                    d.execute("UPDATE remote_guest_ops SET state='TIMED_OUT',completed_at=?, "
                        "last_error='Guest execution outcome unknown; not automatically retried' "
                        "WHERE id=? AND state=?",(stamp,row['id'],row['state']))

    def claim(self, vm_id):
        self.recover_stale(vm_id)
        with self.db() as d:
            # Do not dispatch a second privileged operation while an earlier
            # delivery may still be executing on this VM.
            busy=d.execute("SELECT 1 FROM remote_guest_ops WHERE vm_id=? "
                "AND state IN ('CLAIMED','RUNNING') LIMIT 1",(vm_id,)).fetchone()
            if busy:return None
            row=d.execute("SELECT * FROM remote_guest_ops WHERE vm_id=? AND state='QUEUED' "
                "ORDER BY created_at,id LIMIT 1",(vm_id,)).fetchone()
            if not row:return None
            changed=d.execute("UPDATE remote_guest_ops SET state='CLAIMED',claimed_at=?, "
                "started_at=NULL,attempt_count=attempt_count+1,last_error=NULL "
                "WHERE id=? AND state='QUEUED'",(utcnow(),row['id']))
            if changed.rowcount!=1:return None
            return {'id':row['id'],'action':row['action'],'args':json.loads(row['args_json'])}

    def started(self, vm_id, op_id):
        with self.db() as d:
            changed=d.execute("UPDATE remote_guest_ops SET state='RUNNING',started_at=COALESCE(started_at,?) "
                "WHERE vm_id=? AND id=? AND state='CLAIMED'",(utcnow(),vm_id,op_id))
            if changed.rowcount: return True
            prior=d.execute('SELECT state FROM remote_guest_ops WHERE vm_id=? AND id=?',(vm_id,op_id)).fetchone()
            return bool(prior and prior['state'] in ('RUNNING','SUCCEEDED','FAILED'))

    def complete(self, vm_id, op_id, ok, result):
        if not isinstance(result,dict): result={'message':'Invalid result'}
        raw=json.dumps(result,ensure_ascii=False)
        if len(raw.encode('utf-8'))>MAX_RESULT:
            result={'message':'Result exceeded limit and was discarded'};ok=False
        upload_file=None
        with self.db() as d:
            # Older Agent versions incorrectly report foreground launches as
            # failed when exit_code=None (the child is still alive). The
            # authenticated Agent's launched + positive PID is authoritative
            # for *launch acceptance*, not for application health.
            metadata=d.execute('SELECT action,args_json FROM remote_guest_ops WHERE id=? AND vm_id=?',
                               (op_id,vm_id)).fetchone()
            if metadata and metadata['action']=='COMMAND' and result.get('launched') is True:
                try:
                    args=json.loads(metadata['args_json'])
                    if (args.get('run_mode') in ('interactive','detached') and
                        isinstance(result.get('pid'),int) and result['pid']>0 and
                        not (result.get('process_alive') is False and
                             isinstance(result.get('exit_code'),int) and result['exit_code']!=0)):
                        ok=True
                except (ValueError,TypeError,AttributeError):pass
            # A wait-mode timeout means the outcome is unknown: the process
            # may have performed side effects before the deadline. Never
            # re-dispatch it automatically or mark it as definitively failed.
            timeout_wait=False
            if metadata and metadata['action']=='COMMAND' and result.get('error')=='TimeoutExpired':
                try:timeout_wait=json.loads(metadata['args_json']).get('run_mode','wait')=='wait'
                except (ValueError,TypeError,AttributeError):pass
            terminal='TIMED_OUT' if timeout_wait else ('SUCCEEDED' if ok else 'FAILED')
            # A late result from a previously connected, authenticated guest is
            # authoritative. Safe diagnostics can have been requeued meanwhile.
            changed=d.execute("UPDATE remote_guest_ops SET state=?,result_json=?,completed_at=?,last_error=NULL "
                "WHERE id=? AND vm_id=? AND state IN ('CLAIMED','RUNNING','TIMED_OUT')",
                (terminal,json.dumps(result,ensure_ascii=False),utcnow(),op_id,vm_id))
            if changed.rowcount!=1:
                prior=d.execute('SELECT state,action FROM remote_guest_ops WHERE id=? AND vm_id=?',(op_id,vm_id)).fetchone()
                if prior and prior['state'] in ('SUCCEEDED','FAILED'):return
                if prior and prior['state']=='QUEUED' and prior['action'] in ('DIAGNOSTIC','FILES_LIST','FILE_READ'):
                    d.execute("UPDATE remote_guest_ops SET state=?,result_json=?,completed_at=?,last_error=NULL WHERE id=? AND vm_id=? AND state='QUEUED'",
                        ('SUCCEEDED' if ok else 'FAILED',json.dumps(result,ensure_ascii=False),utcnow(),op_id,vm_id))
                    return
                raise RemoteError('Unknown or unauthorized operation')
            meta=d.execute('SELECT action,args_json FROM remote_guest_ops WHERE id=? AND vm_id=?',(op_id,vm_id)).fetchone()
            if meta and meta['action']=='UPLOAD':
                key=json.loads(meta['args_json']).get('storage_key',op_id)
                if re.fullmatch(r'[a-f0-9-]{36}',key):
                    upload_file=self.files/(key+'.bin')
        # After commit: no longer needed on the host, whether Guest succeeded or
        # failed. Keeping large staging files indefinitely exhausts host disk.
        if upload_file is not None:
            try:upload_file.unlink(missing_ok=True)
            except OSError:pass

    def get(self, op_id):
        with self.db() as d:
            r=d.execute('SELECT * FROM remote_guest_ops WHERE id=?',(op_id,)).fetchone()
            if not r:return None
            a=dict(r)
            a['args']=json.loads(a.pop('args_json'))
            a['result']=json.loads(a.pop('result_json') or '{}')
            if a['action']=='COMMAND':a['args']={'shell':a['args'].get('shell'),'command':'[redacted]'}
            return a

    def list(self, vm_id=None, limit=50, offset=0):
        limit=max(1,min(int(limit),100));offset=max(0,int(offset))
        with self.db() as d:
            if vm_id: rows=d.execute('SELECT id FROM remote_guest_ops WHERE vm_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                (vm_id,limit,offset)).fetchall()
            else: rows=d.execute('SELECT id FROM remote_guest_ops ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                (limit,offset)).fetchall()
        return [self.get(r['id']) for r in rows]

    def count(self,vm_id=None):
        with self.db() as d:
            if vm_id:return d.execute('SELECT COUNT(*) FROM remote_guest_ops WHERE vm_id=?',(vm_id,)).fetchone()[0]
            return d.execute('SELECT COUNT(*) FROM remote_guest_ops').fetchone()[0]

    def create_upload_from_path(self, vm_id, filename, source_file: Path,
                                size: int, sha256: str, directory=DEFAULT_UPLOAD_DIR):
        """Publish a complete streamed upload atomically, before task is queued."""
        validate_upload_filename(filename)
        directory=validate_upload_directory(directory)
        if not isinstance(size,int) or size<0 or not re.fullmatch(r'[a-f0-9]{64}',sha256):
            raise RemoteError('Invalid file metadata')
        storage_key=str(uuid.uuid4())
        final=self.files/(storage_key+'.bin')
        os.replace(source_file, final)
        try:
            op_id=self.queue(vm_id,'UPLOAD',{'filename':filename,'sha256':sha256,
                 'bytes':size,'directory':directory,'storage_key':storage_key},ttl=24*3600)
        except Exception:
            final.unlink(missing_ok=True)
            raise
        return op_id

    def create_upload(self, vm_id, filename, data: bytes, directory=DEFAULT_UPLOAD_DIR):
        """Backward-compatible helper for small test payloads; HTTP uses streaming."""
        temp=self.files/(str(uuid.uuid4())+'.partial')
        try:
            temp.write_bytes(data)
            return self.create_upload_from_path(vm_id,filename,temp,len(data),hashlib.sha256(data).hexdigest(),directory)
        finally:temp.unlink(missing_ok=True)

    def upload_file(self, vm_id, op_id):
        with self.db() as d:
            r=d.execute("SELECT vm_id,state,action,args_json FROM remote_guest_ops WHERE id=?",(op_id,)).fetchone()
            if not r or r['vm_id']!=vm_id or r['action']!='UPLOAD' or r['state'] not in ('CLAIMED','RUNNING'):
                raise RemoteError('Upload not assigned to this guest')
        key=json.loads(r['args_json']).get('storage_key',op_id)
        if not re.fullmatch(r'[a-f0-9-]{36}',key):raise RemoteError('Invalid upload storage key')
        p=self.files/(key+'.bin')
        if not p.is_file():raise RemoteError('Upload payload unavailable')
        return p

    def _link_event(self, db, vm_id, event, reason=''):
        db.execute('INSERT INTO remote_guest_link_events(vm_id,event,reason,recorded_at) VALUES(?,?,?,?)',
                   (vm_id,event,str(reason)[:120],utcnow()))
        # A bound history is enough for troubleshooting; never leak agent keys/URLs.
        db.execute('DELETE FROM remote_guest_link_events WHERE id IN ('
                   'SELECT id FROM remote_guest_link_events WHERE vm_id=? ORDER BY id DESC LIMIT -1 OFFSET 100)',(vm_id,))

    def link_connected(self, vm_id, connection_id):
        with self.db() as d:
            prior=d.execute('SELECT state,last_wss_heartbeat FROM remote_guest_links WHERE vm_id=?',(vm_id,)).fetchone()
            if prior and prior['state']=='connected' and prior['last_wss_heartbeat']:
                self._link_event(d,vm_id,'reconnected','Previous socket replaced')
            else:
                self._link_event(d,vm_id,'connected')
            d.execute('INSERT INTO remote_guest_links(vm_id,connection_id,state,connected_at,last_wss_heartbeat,disconnected_at,disconnect_reason) '
                'VALUES(?,?,?,?,?,?,?) ON CONFLICT(vm_id) DO UPDATE SET '
                'connection_id=excluded.connection_id,state=excluded.state,connected_at=excluded.connected_at,'
                'last_wss_heartbeat=excluded.last_wss_heartbeat',
                (vm_id,connection_id,'connected',utcnow(),utcnow(),None,None))

    def link_heartbeat(self, vm_id, connection_id):
        with self.db() as d:
            d.execute("UPDATE remote_guest_links SET last_wss_heartbeat=? WHERE vm_id=? AND connection_id=? AND state='connected'",
                      (utcnow(),vm_id,connection_id))

    def link_disconnected(self, vm_id, connection_id, reason):
        with self.db() as d:
            changed=d.execute("UPDATE remote_guest_links SET state='disconnected',disconnected_at=?,disconnect_reason=? "
                "WHERE vm_id=? AND connection_id=? AND state='connected'",
                (utcnow(),str(reason)[:120],vm_id,connection_id))
            if changed.rowcount:self._link_event(d,vm_id,'disconnected',str(reason)[:120])

    def link_client_error(self, vm_id, reason):
        allowed={'TLS_VERIFY_FAILED','CONNECTION_REFUSED','CONNECTION_TIMED_OUT',
                 'WEBSOCKET_ERROR','IDENTITY_MISMATCH','OS_ERROR','GATEWAY_UNAVAILABLE','UNKNOWN_ERROR'}
        if reason not in allowed:reason='UNKNOWN_ERROR'
        with self.db() as d:
            d.execute('UPDATE remote_guest_links SET last_client_error=?,last_client_error_at=? WHERE vm_id=?',
                (reason,utcnow(),vm_id))
            self._link_event(d,vm_id,'client_error',reason)

    def link_states(self):
        """One batched query for fleet overview WSS indicators."""
        with self.db() as d:
            rows=[dict(r) for r in d.execute(
                'SELECT vm_id,state,last_wss_heartbeat,disconnected_at,disconnect_reason '
                'FROM remote_guest_links')]
        result={}
        for a in rows:
            if a['state']=='connected':
                try: age=(datetime.now(timezone.utc)-datetime.fromisoformat(a['last_wss_heartbeat'])).total_seconds()
                except (TypeError,ValueError):age=1e9
                if age>75:a['state']='heartbeat_timeout'
            result[a['vm_id']]=a
        return result

    def link_presence(self, vm_id, history_limit=12):
        with self.db() as d:
            r=d.execute('SELECT * FROM remote_guest_links WHERE vm_id=?',(vm_id,)).fetchone()
            events=[dict(x) for x in d.execute(
                'SELECT event,reason,recorded_at FROM remote_guest_link_events WHERE vm_id=? ORDER BY id DESC LIMIT ?',
                (vm_id,max(0,min(50,int(history_limit)))))]
        a=dict(r) if r else {'vm_id':vm_id,'state':'never_connected','last_wss_heartbeat':None,
                               'disconnect_reason':None,'last_client_error':None}
        a.pop('connection_id',None)
        if a['state']=='connected':
            try:
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(a['last_wss_heartbeat'])).total_seconds()
            except (ValueError,TypeError):age=1e9
            if age>75:
                a['state']='heartbeat_timeout'
                a['disconnect_reason']='No WSS heartbeat within 75 seconds; link may be lost'
        a['history']=events
        return a

    def change_fingerprint(self):
        """Lightweight monotonic-ish snapshot for an authenticated admin SSE stream.

        The stream carries no command outputs or secrets; it only wakes the UI
        which must perform its normal authenticated read.
        """
        with self.db() as d:
            ops=[tuple(row) for row in d.execute(
                "SELECT id,state,COALESCE(started_at,''),COALESCE(completed_at,''),COALESCE(last_error,'') "
                'FROM remote_guest_ops ORDER BY created_at DESC,id DESC LIMIT 30')]
            links=[tuple(row) for row in d.execute(
                "SELECT vm_id,state,COALESCE(last_wss_heartbeat,''),COALESCE(disconnected_at,'') "
                'FROM remote_guest_links ORDER BY vm_id')]
            apps=[tuple(row) for row in d.execute(
                'SELECT vm_id,app_id,updated_at FROM remote_guest_apps ORDER BY vm_id,app_id')]
        return (ops,links,apps)

    @staticmethod
    def _source_time(value):
        if not isinstance(value,str) or len(value)>48:return None
        try:
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None:return None
            # Host clock is authoritative for ordering; tolerate limited drift.
            if parsed > datetime.now(timezone.utc)+timedelta(minutes=10):return None
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:return None

    def app_report(self, vm_id: str, apps: list):
        if not isinstance(apps,list) or len(apps)>len(APP_IDS):raise RemoteError('Invalid app list')
        rows=[]
        for a in apps:
            if not isinstance(a,dict) or a.get('app_id') not in APP_IDS or not is_safe_app_url(a.get('url','')):
                raise RemoteError('Invalid app endpoint')
            reported=self._source_time(a.get('reported_at'))
            rows.append((vm_id,a['app_id'],a['url'],str(a.get('version',''))[:40],
                         str(a.get('auth_state','unconfigured'))[:40],utcnow(),int(a.get('online',False)),reported))
        with self.db() as d:
            for r in rows:
                # Never erase the real report timestamp with an automatic heartbeat.
                # On reconnect, reject an older app report for the same VM/app.
                d.execute('''INSERT INTO remote_guest_apps(vm_id,app_id,url,version,auth_state,updated_at,is_available,reported_at)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(vm_id,app_id) DO UPDATE SET
                    url=CASE WHEN excluded.reported_at IS NULL OR remote_guest_apps.reported_at IS NULL OR excluded.reported_at >= remote_guest_apps.reported_at THEN excluded.url ELSE remote_guest_apps.url END,
                    version=CASE WHEN excluded.reported_at IS NULL OR remote_guest_apps.reported_at IS NULL OR excluded.reported_at >= remote_guest_apps.reported_at THEN excluded.version ELSE remote_guest_apps.version END,
                    auth_state=CASE WHEN excluded.reported_at IS NULL OR remote_guest_apps.reported_at IS NULL OR excluded.reported_at >= remote_guest_apps.reported_at THEN excluded.auth_state ELSE remote_guest_apps.auth_state END,
                    reported_at=CASE WHEN excluded.reported_at IS NOT NULL AND (remote_guest_apps.reported_at IS NULL OR excluded.reported_at >= remote_guest_apps.reported_at) THEN excluded.reported_at ELSE remote_guest_apps.reported_at END,
                    updated_at=excluded.updated_at,is_available=excluded.is_available''',r)

    def apps(self, vm_id):
        with self.db() as d:
            return [dict(r) for r in d.execute('''SELECT a.*,s.source_reported_at AS secret_reported_at
                FROM remote_guest_apps a LEFT JOIN remote_app_secrets s
                ON a.vm_id=s.vm_id AND a.app_id=s.app_id
                WHERE a.vm_id=? ORDER BY a.app_id''',(vm_id,))]

    def app_url(self, vm_id, app_id):
        with self.db() as d:
            r=d.execute('SELECT url FROM remote_guest_apps WHERE vm_id=? AND app_id=?',(vm_id,app_id)).fetchone()
            return r['url'] if r else None

    def app_secret(self,vm_id,app_id,secret=None,reported_at=None):
        if app_id not in APP_IDS: raise RemoteError('Unsupported app')
        if secret is not None:
            if not isinstance(secret,str) or not secret or len(secret)>512:
                raise RemoteError('Invalid access code')
            source_time=self._source_time(reported_at)
            with self.db() as d:
                old=d.execute('''SELECT sealed_code,source_reported_at FROM remote_app_secrets
                    WHERE vm_id=? AND app_id=?''',(vm_id,app_id)).fetchone()
                if old:
                    prev_source=old['source_reported_at']
                    # Old DBs only had changed_at=last WSS replay time. It was
                    # NOT an original report timestamp, so never compare to it.
                    if prev_source and (source_time is None or source_time < prev_source):
                        return None
                    try:old_code=dpapi(old['sealed_code'],decrypt=True).decode('utf-8')
                    except (RuntimeError,OSError,ValueError,UnicodeError):old_code=None
                    if secret==old_code and source_time==prev_source:return None
                d.execute('''INSERT INTO remote_app_secrets(vm_id,app_id,sealed_code,changed_at,source_reported_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(vm_id,app_id) DO UPDATE SET
                    sealed_code=excluded.sealed_code,changed_at=excluded.changed_at,
                    source_reported_at=excluded.source_reported_at''',
                    (vm_id,app_id,dpapi(secret.encode('utf-8')),utcnow(),source_time))
            return None
        with self.db() as d:
            r=d.execute('SELECT sealed_code FROM remote_app_secrets WHERE vm_id=? AND app_id=?',(vm_id,app_id)).fetchone()
            return dpapi(r['sealed_code'],decrypt=True).decode('utf-8') if r else None


    def revoke_app_sessions(self, service_ref: str):
        with self.db() as d:
            d.execute('DELETE FROM remote_app_sessions WHERE service_ref=?',(service_ref,))
            d.execute('DELETE FROM remote_app_route_sessions WHERE route_id IN '
                      '(SELECT route_id FROM remote_app_routes WHERE service_ref=?)',(service_ref,))
            # Keep the opaque route assignment across logout. It is a stable
            # application mount, never an authentication token. Service state
            # is still checked for every request, and all sessions are revoked.
            d.execute('DELETE FROM remote_app_launches WHERE service_ref=?',(service_ref,))

    def issue_app_launch(self, service_ref: str, vm_id: str, app_id: str) -> str:
        """One-time 60-second browser handoff. Never return Guest IP/port."""
        if app_id not in APP_IDS or not self.app_url(vm_id,app_id):
            raise RemoteError('Application has not reported its local entry point')
        with self.db() as d:
            live=d.execute('SELECT is_available FROM remote_guest_apps WHERE vm_id=? AND app_id=?',(vm_id,app_id)).fetchone()
            if not live or not live['is_available']:
                raise RemoteError('Application is not currently available')
        token=secrets.token_urlsafe(32)
        now=int(time.time())
        with self.db() as d:
            if not d.execute("SELECT 1 FROM business_services WHERE service_ref=? AND vm_id=? AND status='ACTIVE' AND (expires_at IS NULL OR expires_at>strftime('%Y-%m-%dT%H:%M:%SZ','now'))",(service_ref,vm_id)).fetchone():
                raise RemoteError('Service no longer active')
            d.execute('DELETE FROM remote_app_launches WHERE expires_at<?',(now,))
            d.execute('INSERT INTO remote_app_launches VALUES (?,?,?,?,?)',
                      (hashlib.sha256(token.encode()).hexdigest(),service_ref,vm_id,app_id,now+60))
        return token

    def exchange_app_launch(self, ticket: str):
        if not isinstance(ticket,str) or not 20<=len(ticket)<=256:
            raise RemoteError('Invalid launch ticket')
        now=int(time.time())
        with self.db() as d:
            key=hashlib.sha256(ticket.encode()).hexdigest()
            r=d.execute('SELECT * FROM remote_app_launches WHERE ticket_hash=?',(key,)).fetchone()
            if r:d.execute('DELETE FROM remote_app_launches WHERE ticket_hash=?',(key,))
            if not r or r['expires_at']<=now:
                raise RemoteError('Launch ticket expired or already used')
            if not d.execute("SELECT 1 FROM business_services WHERE service_ref=? AND vm_id=? AND status='ACTIVE' AND (expires_at IS NULL OR expires_at>strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                             (r['service_ref'],r['vm_id'])).fetchone():
                raise RemoteError('Service suspended or terminated')
            route=d.execute('SELECT route_id FROM remote_app_routes '
                            'WHERE service_ref=? AND vm_id=? AND app_id=?',
                            (r['service_ref'],r['vm_id'],r['app_id'])).fetchone()
            if route:
                relay_id=route['route_id']
            else:
                # Long-lived opaque path, not a credential. Never derive it
                # from a predictable customer, VM or business identifier.
                relay_id=secrets.token_urlsafe(18)
                d.execute('INSERT INTO remote_app_routes VALUES (?,?,?,?)',
                          (relay_id,r['service_ref'],r['vm_id'],r['app_id']))
            secret=secrets.token_urlsafe(32)
            d.execute('DELETE FROM remote_app_route_sessions WHERE expires_at<?',(now,))
            d.execute('INSERT INTO remote_app_route_sessions VALUES (?,?,?)',
                      (relay_id,hashlib.sha256(secret.encode()).hexdigest(),now+3*3600))
            return relay_id,r['app_id'],secret

    def resolve_app_session(self, relay_id: str, app_id: str, cookie: str):
        if not 12<=len(relay_id)<=45 or app_id not in APP_IDS or not cookie or len(cookie)>256:
            raise RemoteError('Invalid application session')
        with self.db() as d:
            digest=hashlib.sha256(cookie.encode()).hexdigest()
            route=d.execute('SELECT * FROM remote_app_routes WHERE route_id=? AND app_id=?',
                            (relay_id,app_id)).fetchone()
            if route:
                # Explicit scoped session lookup. Never accept another app's
                # cookie even when the route identifier is visible.
                active=d.execute('SELECT expires_at FROM remote_app_route_sessions '
                                 'WHERE route_id=? AND session_hash=?',(relay_id,digest)).fetchone()
                if not active or active['expires_at']<=int(time.time()):
                    raise RemoteError('Application session expired')
                r=route
            else:
                # Allow already-opened pre-upgrade sessions to finish naturally.
                r=d.execute('SELECT * FROM remote_app_sessions WHERE relay_id=? AND app_id=?',
                            (relay_id,app_id)).fetchone()
                if not r or r['expires_at']<=int(time.time()) or not secrets.compare_digest(
                    r['session_hash'],digest):
                    raise RemoteError('Application session expired')
            if not d.execute("SELECT 1 FROM business_services WHERE service_ref=? AND vm_id=? AND status='ACTIVE' AND (expires_at IS NULL OR expires_at>strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                             (r['service_ref'],r['vm_id'])).fetchone():
                raise RemoteError('Application service inactive')
            return r['vm_id']
