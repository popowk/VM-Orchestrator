"""Tenant-scoped administrator-approved customer operations."""
from __future__ import annotations
import json, re, sqlite3, threading, uuid
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel, ConfigDict, Field
from .remote_ops import MAX_COMMAND_CMD, MAX_COMMAND_POWERSHELL, MAX_COMMAND_BYTES

PRODUCT_RE=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$')


def utcnow():return datetime.now(timezone.utc).isoformat(timespec='seconds')

class ActionInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str|None=None
    product_ref:str=Field(min_length=1,max_length=96)
    label:str=Field(min_length=1,max_length=80)
    description:str=Field(default='',max_length=300)
    kind:str
    enabled:bool=False
    shell:str='powershell'
    command:str=Field(default='',max_length=MAX_COMMAND_POWERSHELL)
    run_mode:str='wait'
    cwd:str=''
    timeout_seconds:int=Field(default=60,ge=5,le=300)
    package_id:str=''
    install_dir:str=''
    sort_order:int=Field(default=100,ge=0,le=100000)

class ToggleInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled:bool

class ExecuteInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id:uuid.UUID
    confirm:bool

class SelfServiceStore:
    def __init__(self,path:Path,lock=None,remote_store=None):
        self.path=Path(path);self.lock=lock or threading.RLock();self.remote_store=remote_store
        with self.db() as db:
            db.executescript("""
        CREATE TABLE IF NOT EXISTS business_self_actions (
          id TEXT PRIMARY KEY,product_ref TEXT NOT NULL,label TEXT NOT NULL,
          description TEXT NOT NULL,kind TEXT NOT NULL,enabled INTEGER NOT NULL,
          shell TEXT NOT NULL,command TEXT NOT NULL,run_mode TEXT NOT NULL,
          cwd TEXT NOT NULL,timeout_seconds INTEGER NOT NULL,package_id TEXT NOT NULL,
          install_dir TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS self_actions_product ON business_self_actions(product_ref,enabled);
        CREATE TABLE IF NOT EXISTS business_self_executions (
          request_id TEXT PRIMARY KEY,service_ref TEXT NOT NULL,vm_id TEXT NOT NULL,
          action_id TEXT NOT NULL,action_kind TEXT NOT NULL,operation_id TEXT,
          status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          error TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS self_exec_service ON business_self_executions(service_ref,created_at);
        """)
            cols={r["name"] for r in db.execute("PRAGMA table_info(business_self_actions)")}
            if "sort_order" not in cols:db.execute("ALTER TABLE business_self_actions ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 100")
            execution_cols={r["name"] for r in db.execute("PRAGMA table_info(business_self_executions)")}
            if "action_label" not in execution_cols:
                db.execute("ALTER TABLE business_self_executions ADD COLUMN action_label TEXT NOT NULL DEFAULT ''")
            # Historical entries retain a readable label even if the operation is later removed.
            db.execute("""UPDATE business_self_executions SET action_label=COALESCE(
                (SELECT label FROM business_self_actions WHERE id=business_self_executions.action_id), '')
                WHERE action_label=''""")

    @contextmanager
    def db(self):
        d=sqlite3.connect(str(self.path),timeout=20);d.row_factory=sqlite3.Row
        try:
            d.execute('PRAGMA busy_timeout=20000')
            with d:yield d
        finally:d.close()

    def list(self,product_ref=None,enabled_only=False):
        q='SELECT * FROM business_self_actions WHERE 1=1';args=[]
        if product_ref is not None:q+=' AND product_ref=?';args.append(product_ref)
        if enabled_only:q+=' AND enabled=1'
        with self.db() as d:return [dict(x) for x in d.execute(q+' ORDER BY sort_order ASC,created_at ASC,id ASC',args)]

    def get(self,action_id):
        with self.db() as d:r=d.execute('SELECT * FROM business_self_actions WHERE id=?',(action_id,)).fetchone()
        return dict(r) if r else None

    @staticmethod
    def public(r):
        return {k:r[k] for k in ('id','label','description','kind','updated_at','sort_order')}

    def save(self,p:ActionInput):
        if not PRODUCT_RE.fullmatch(p.product_ref) or not p.label.strip() or any(ord(c)<32 for c in p.label+p.description):
            raise ValueError('Invalid product, title or description')
        if p.kind not in ('COMMAND','SOFTWARE_UPDATE'):raise ValueError('Unsupported operation type')
        if p.kind=='COMMAND':
            if p.shell not in ('cmd','powershell') or p.run_mode not in ('wait','detached','interactive'):
                raise ValueError('Invalid command shell or mode')
            limit=MAX_COMMAND_CMD if p.shell=='cmd' else MAX_COMMAND_POWERSHELL
            if (not 1<=len(p.command)<=limit or '\x00' in p.command
                    or p.package_id or p.install_dir):
                raise ValueError(f'预设命令无效：CMD 最多 {MAX_COMMAND_CMD} 字符，PowerShell 最多 {MAX_COMMAND_POWERSHELL} 字符')
            # Match the remote COMMAND transport cap at save time as well,
            # so a preset cannot be accepted but rejected at dispatch.
            params={'shell':p.shell,'command':p.command,'privilege':'system',
                    'confirmed':True,'run_mode':p.run_mode,'cwd':p.cwd,
                    'timeout_seconds':p.timeout_seconds}
            encoded=json.dumps(params,ensure_ascii=False,separators=(',',':'))
            if len(encoded.encode('utf-8'))>MAX_COMMAND_BYTES:
                raise ValueError(f'预设命令超过远程运维传输限制（{MAX_COMMAND_BYTES} UTF-8 字节）')
        elif not p.package_id or p.command or p.cwd:
            raise ValueError('Update operations require a pinned package and no command or cwd')
        id_=p.id or str(uuid.uuid4());now=utcnow()
        if p.id:
            try:uuid.UUID(p.id)
            except ValueError:raise ValueError('Invalid operation ID')
        with self.lock,self.db() as d:
            if p.id:
                original=d.execute('SELECT product_ref FROM business_self_actions WHERE id=?',(id_,)).fetchone()
                if not original:raise LookupError('Operation not found')
                if original['product_ref']!=p.product_ref:raise ValueError('Cannot change the owning product')
                d.execute('''UPDATE business_self_actions SET label=?,description=?,kind=?,enabled=?,shell=?,command=?,run_mode=?,cwd=?,timeout_seconds=?,package_id=?,install_dir=?,sort_order=?,updated_at=? WHERE id=?''',
                    (p.label.strip(),p.description.strip(),p.kind,int(p.enabled),p.shell,p.command,p.run_mode,p.cwd,p.timeout_seconds,p.package_id,p.install_dir,p.sort_order,now,id_))
            else:
                d.execute('''INSERT INTO business_self_actions (id,product_ref,label,description,kind,enabled,shell,command,run_mode,cwd,timeout_seconds,package_id,install_dir,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (id_,p.product_ref,p.label.strip(),p.description.strip(),p.kind,int(p.enabled),p.shell,p.command,p.run_mode,p.cwd,p.timeout_seconds,p.package_id,p.install_dir,p.sort_order,now,now))
        return self.get(id_)

    def toggle(self,id_,enabled):
        with self.lock,self.db() as d:
            x=d.execute('UPDATE business_self_actions SET enabled=?,updated_at=? WHERE id=?',(int(enabled),utcnow(),id_))
            if not x.rowcount:raise LookupError('Operation not found')
        return self.get(id_)

    def delete(self,id_):
        with self.lock,self.db() as d:
            if not d.execute('DELETE FROM business_self_actions WHERE id=?',(id_,)).rowcount:
                raise LookupError('Operation not found')

    def executions(self,service_ref):
        """Only the five newest operations appear in the customer history."""
        with self.db() as d:
            return [dict(r) for r in d.execute(
                "SELECT * FROM business_self_executions WHERE service_ref=? ORDER BY created_at DESC, rowid DESC LIMIT 5",
                (service_ref,))]

    def blocking_execution(self,service_ref,vm_id):
        """Only actual in-flight dispatches block another operation.

        Unknown, timed-out and pending-review results are historical information;
        they do not block subsequent customer actions.
        """
        with self.db() as d:
            row=d.execute("""SELECT request_id,action_id,action_label,status,created_at
                FROM business_self_executions WHERE service_ref=? AND vm_id=?
                AND status IN ('RESERVING','QUEUED','CLAIMED','RUNNING')
                ORDER BY created_at DESC,rowid DESC LIMIT 1""",(service_ref,vm_id)).fetchone()
            return dict(row) if row else None

    def admin_executions(self):
        with self.db() as d:
            in_flight=[tuple(r) for r in d.execute("SELECT DISTINCT service_ref,vm_id FROM business_self_executions WHERE status IN ('RESERVING','QUEUED','CLAIMED','RUNNING','UNKNOWN','TIMED_OUT','VERIFY_REQUIRED') OR (status='FAILED' AND action_kind='COMMAND')")]
        for service_ref,vm_id in in_flight:self.reconcile(service_ref,vm_id)
        with self.db() as d:
            # Always surface review-required records, even if older than 50.
            return [dict(r) for r in d.execute("""SELECT request_id,service_ref,vm_id,action_id,action_kind,status,created_at,updated_at
                FROM business_self_executions
                WHERE status IN ('UNKNOWN','TIMED_OUT','VERIFY_REQUIRED')
                   OR request_id IN (SELECT request_id FROM business_self_executions ORDER BY created_at DESC LIMIT 50)
                ORDER BY CASE WHEN status IN ('UNKNOWN','TIMED_OUT','VERIFY_REQUIRED') THEN 0 ELSE 1 END,created_at DESC LIMIT 200""")]

    def acknowledge(self,request_id):
        """Manual exception clearance AFTER an administrator inspects VM logs.

        The operation itself is never retried or cancelled by this acknowledgement.
        """
        with self.lock,self.db() as d:
            r=d.execute('SELECT * FROM business_self_executions WHERE request_id=?',(request_id,)).fetchone()
            if not r:raise LookupError('Execution record not found')
            if r['status'] not in ('UNKNOWN','TIMED_OUT','VERIFY_REQUIRED'):
                raise ValueError('Only unknown or unverified outcomes may be acknowledged')
            # A request might have crashed after dispatch but before storing
            # operation_id. Never unblock while an old command/update can run.
            if d.execute("SELECT 1 FROM remote_guest_ops WHERE vm_id=? AND action='COMMAND' AND state IN ('QUEUED','CLAIMED','RUNNING') LIMIT 1",(r['vm_id'],)).fetchone():
                raise ValueError('仍有待下发或正在执行的 Guest 命令，请等待结束或超时后再核对')
            if d.execute("SELECT 1 FROM guest_tasks WHERE vm_id=? AND status IN ('QUEUED','CLAIMED','RUNNING') LIMIT 1",(r['vm_id'],)).fetchone():
                raise ValueError('仍有待处理的软件更新任务，不允许解除阻塞')
            d.execute("UPDATE business_self_executions SET status='ACKNOWLEDGED',updated_at=? WHERE request_id=?",
                      (utcnow(),request_id))
            return dict(r)

    def reserve(self,service_ref,vm_id,action_id,kind,request_id):
        """Persist idempotency before dispatch; a network retry cannot execute twice."""
        key=str(request_id)
        with self.lock,self.db() as d:
            # Across multiple workers/old services, SQLite must serialize the
            # check and insert, not merely the local Python lock.
            d.execute('BEGIN IMMEDIATE')
            old=d.execute('SELECT * FROM business_self_executions WHERE request_id=?',(key,)).fetchone()
            if old:
                if old['service_ref']!=service_ref or old['action_id']!=action_id:
                    raise ValueError('Request ID belongs to a different operation')
                return dict(old),False
            # Admin review is audit-only. Do not treat an UNKNOWN, TIMED_OUT
            # or VERIFY_REQUIRED execution as a VM mutex. Only commands still
            # reserved/queued/claimed/running prevent concurrent dispatch.
            blocking=d.execute("SELECT status FROM business_self_executions WHERE vm_id=? AND status IN ('RESERVING','QUEUED','CLAIMED','RUNNING') ORDER BY created_at DESC LIMIT 1",(vm_id,)).fetchone()
            if blocking:
                raise RuntimeError('已有自助操作正在下发或执行，请等待完成后再试')
            previous=d.execute('SELECT created_at,status FROM business_self_executions WHERE vm_id=? AND action_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(vm_id,action_id)).fetchone()
            # A review-only result must not impose even the short cooldown.
            if previous and previous['status'] not in ('UNKNOWN','TIMED_OUT','VERIFY_REQUIRED','ACKNOWLEDGED') and (datetime.now(timezone.utc)-datetime.fromisoformat(previous['created_at'])).total_seconds()<30:
                raise RuntimeError('同一个操作两次执行至少间隔 30 秒，请稍后重试')
            action=d.execute('SELECT label FROM business_self_actions WHERE id=?',(action_id,)).fetchone()
            if not action:raise LookupError('Self-service action no longer exists')
            d.execute("INSERT INTO business_self_executions(request_id,service_ref,vm_id,action_id,action_kind,action_label,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'RESERVING',?,?)",
                      (key,service_ref,vm_id,action_id,kind,action['label'],utcnow(),utcnow()))
        return {'request_id':key,'status':'RESERVING'},True

    def record(self,request_id,status,operation_id=None,error=''):
        with self.lock,self.db() as d:
            d.execute('UPDATE business_self_executions SET status=?,operation_id=COALESCE(?,operation_id),updated_at=?,error=? WHERE request_id=?',
                      (status,operation_id,utcnow(),error[:180],str(request_id)))

    def reconcile(self,service_ref,vm_id):
        """Synchronize job status without ever replaying an uncertain operation.

        Expired never-claimed jobs can be safely closed, whereas CLAIMED/RUNNING
        jobs with lost results require an administrator to inspect the Guest.
        """
        if self.remote_store is not None:
            # This is essential: without recovery, abandoned remote CLAIMED /
            # RUNNING / QUEUED entries can block self-service indefinitely.
            self.remote_store.recover_stale(vm_id)
        with self.lock,self.db() as d:
            # An update still QUEUED after 20 minutes was *never claimed* by
            # Guest. Conditional UPDATE is atomic against concurrent claim().
            # Closing it here prevents a late Guest reconnect from starting a
            # superseded installation unexpectedly.
            expiry=(datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat()
            d.execute("UPDATE guest_tasks SET status='CANCELLED',message='Undelivered customer task expired without Guest claim',completed_at=? "
                      "WHERE vm_id=? AND status='QUEUED' AND created_at<? AND id IN "
                      "(SELECT operation_id FROM business_self_executions WHERE vm_id=? AND action_kind='SOFTWARE_UPDATE')",
                      (utcnow(),vm_id,expiry,vm_id))
            # Guest may disappear and never poll fleet.claim() again. Apply
            # the same claimed/running deadlines to customer-owned updates;
            # an uncertain update is never described as a safe failure.
            claim_expiry=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
            run_expiry=(datetime.now(timezone.utc)-timedelta(minutes=90)).isoformat()
            task_scope="AND id IN (SELECT operation_id FROM business_self_executions WHERE vm_id=? AND action_kind='SOFTWARE_UPDATE')"
            d.execute("UPDATE guest_tasks SET status='FAILED',message='Task result unknown after Guest disconnect; inspect Guest before retrying',completed_at=? "
                      "WHERE vm_id=? AND status='CLAIMED' AND COALESCE(claimed_at,created_at)<? "+task_scope,
                      (utcnow(),vm_id,claim_expiry,vm_id))
            d.execute("UPDATE guest_tasks SET status='FAILED',message='Update outcome unknown after lost heartbeat; review Guest update-journals before any retry',completed_at=? "
                      "WHERE vm_id=? AND status='RUNNING' AND COALESCE(progress_at,claimed_at,created_at)<? "+task_scope,
                      (utcnow(),vm_id,run_expiry,vm_id))
            rows=d.execute("SELECT * FROM business_self_executions WHERE service_ref=? AND vm_id=? "
                "AND (status IN ('RESERVING','QUEUED','CLAIMED','RUNNING','UNKNOWN','TIMED_OUT','VERIFY_REQUIRED') "
                "OR (status='FAILED' AND action_kind='COMMAND'))",(service_ref,vm_id)).fetchall()
            for r in rows:
                if not r['operation_id']:
                    # A crash between reserving the request and saving the
                    # dispatched task is uncertain, never safe to auto-retry.
                    if (r['status']=='RESERVING' and
                        (datetime.now(timezone.utc)-datetime.fromisoformat(r['created_at'])).total_seconds()>120):
                        d.execute("UPDATE business_self_executions SET status='UNKNOWN',updated_at=? WHERE request_id=?",
                                  (utcnow(),r['request_id']))
                    continue
                if r['action_kind']=='COMMAND':
                    task=d.execute('SELECT state AS s,args_json,result_json FROM remote_guest_ops WHERE id=? AND vm_id=?',
                                   (r['operation_id'],vm_id)).fetchone()
                else:
                    task=d.execute('SELECT status AS s,message FROM guest_tasks WHERE id=? AND vm_id=?',
                                   (r['operation_id'],vm_id)).fetchone()
                status=task['s'] if task else 'UNKNOWN'
                if task and r['action_kind']=='COMMAND' and status in ('FAILED','SUCCEEDED'):
                    # Preserve legacy interactive-start fix: PID confirms launch
                    # acceptance only, not application health.
                    try:
                        result=json.loads(task['result_json'] or '{}')
                        params=json.loads(task['args_json'] or '{}')
                        if (params.get('run_mode') in ('interactive','detached') and
                            result.get('launched') is True and
                            isinstance(result.get('pid'),int) and result['pid']>0 and
                            not (result.get('process_alive') is False and
                                 isinstance(result.get('exit_code'),int) and result['exit_code']!=0)):
                            status='SUCCEEDED'
                        elif (status=='FAILED' and params.get('run_mode','wait')=='wait' and
                              result.get('error')=='TimeoutExpired'):
                            status='TIMED_OUT'
                    except (ValueError,TypeError,AttributeError):pass
                if r['action_kind']=='COMMAND':
                    if status=='EXPIRED':status='FAILED' # queue TTL, never delivered
                else:
                    if status=='CANCELLED':status='FAILED' # never-claimed or revoked
                    elif status=='FAILED' and task and (
                        'unknown' in (task['message'] or '').lower() or
                        'review guest' in (task['message'] or '').lower() or
                        'inspect guest' in (task['message'] or '').lower()):
                        status='UNKNOWN'  # host cannot prove an update did not run
                if status!=r['status']:
                    d.execute('UPDATE business_self_executions SET status=?,updated_at=? WHERE request_id=?',
                              (status,utcnow(),r['request_id']))
        return self.executions(service_ref)
