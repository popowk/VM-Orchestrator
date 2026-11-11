"""Tenant-scoped administrator-approved customer operations."""
from __future__ import annotations
import json, re, sqlite3, threading, uuid
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field

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
    command:str=''
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
    def __init__(self,path:Path,lock=None):
        self.path=Path(path);self.lock=lock or threading.RLock()
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
            if not 1<=len(p.command)<=2000 or '\x00' in p.command or p.package_id or p.install_dir:
                raise ValueError('Command operations require only a valid administrator-defined command')
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
        with self.db() as d:
            return [dict(r) for r in d.execute('SELECT * FROM business_self_executions WHERE service_ref=? ORDER BY created_at DESC LIMIT 5',(service_ref,))]

    def admin_executions(self):
        with self.db() as d:
            in_flight=[tuple(r) for r in d.execute("SELECT DISTINCT service_ref,vm_id FROM business_self_executions WHERE status IN ('RESERVING','QUEUED','CLAIMED','RUNNING')")]
        for service_ref,vm_id in in_flight:self.reconcile(service_ref,vm_id)
        with self.db() as d:
            return [dict(r) for r in d.execute('''SELECT request_id,service_ref,vm_id,action_id,action_kind,status,created_at,updated_at
                FROM business_self_executions ORDER BY created_at DESC LIMIT 50''')]

    def acknowledge(self,request_id):
        """Manual exception clearance AFTER an administrator inspects VM logs.

        The operation itself is never retried or cancelled by this acknowledgement.
        """
        with self.lock,self.db() as d:
            r=d.execute('SELECT * FROM business_self_executions WHERE request_id=?',(request_id,)).fetchone()
            if not r:raise LookupError('Execution record not found')
            if r['status'] not in ('UNKNOWN','TIMED_OUT','VERIFY_REQUIRED'):
                raise ValueError('Only unknown or unverified outcomes may be acknowledged')
            d.execute("UPDATE business_self_executions SET status='ACKNOWLEDGED',updated_at=? WHERE request_id=?",
                      (utcnow(),request_id))
            return dict(r)

    def reserve(self,service_ref,vm_id,action_id,kind,request_id):
        """Persist idempotency before dispatch; a network retry cannot execute twice."""
        key=str(request_id)
        with self.lock,self.db() as d:
            old=d.execute('SELECT * FROM business_self_executions WHERE request_id=?',(key,)).fetchone()
            if old:
                if old['service_ref']!=service_ref or old['action_id']!=action_id:
                    raise ValueError('Request ID belongs to a different operation')
                return dict(old),False
            if d.execute("SELECT 1 FROM business_self_executions WHERE vm_id=? AND status IN ('RESERVING','QUEUED','CLAIMED','RUNNING','UNKNOWN','TIMED_OUT','VERIFY_REQUIRED') LIMIT 1",(vm_id,)).fetchone():
                raise RuntimeError('A prior operation is still running or needs administrator review')
            previous=d.execute('SELECT created_at FROM business_self_executions WHERE vm_id=? AND action_id=? ORDER BY created_at DESC LIMIT 1',(vm_id,action_id)).fetchone()
            if previous and (datetime.now(timezone.utc)-datetime.fromisoformat(previous['created_at'])).total_seconds()<30:
                raise RuntimeError('Please wait 30 seconds before repeating this operation')
            d.execute("INSERT INTO business_self_executions(request_id,service_ref,vm_id,action_id,action_kind,status,created_at,updated_at) VALUES(?,?,?,?,?,'RESERVING',?,?)",
                      (key,service_ref,vm_id,action_id,kind,utcnow(),utcnow()))
        return {'request_id':key,'status':'RESERVING'},True

    def record(self,request_id,status,operation_id=None,error=''):
        with self.lock,self.db() as d:
            d.execute('UPDATE business_self_executions SET status=?,operation_id=COALESCE(?,operation_id),updated_at=?,error=? WHERE request_id=?',
                      (status,operation_id,utcnow(),error[:180],str(request_id)))

    def reconcile(self,service_ref,vm_id):
        with self.lock,self.db() as d:
            rows=d.execute("SELECT * FROM business_self_executions WHERE service_ref=? AND vm_id=? "
                "AND (status IN ('RESERVING','QUEUED','CLAIMED','RUNNING') OR "
                "(status='FAILED' AND action_kind='COMMAND'))",(service_ref,vm_id)).fetchall()
            for r in rows:
                if not r['operation_id']:
                    if (datetime.now(timezone.utc)-datetime.fromisoformat(r['created_at'])).total_seconds()>120:
                        d.execute("UPDATE business_self_executions SET status='UNKNOWN',updated_at=? WHERE request_id=?",(utcnow(),r['request_id']))
                    continue
                table='remote_guest_ops' if r['action_kind']=='COMMAND' else 'guest_tasks'
                col='state' if r['action_kind']=='COMMAND' else 'status'
                if r['action_kind']=='COMMAND':
                    task=d.execute('SELECT state AS s,args_json,result_json FROM remote_guest_ops WHERE id=? AND vm_id=?',
                                   (r['operation_id'],vm_id)).fetchone()
                else:
                    task=d.execute(f'SELECT {col} AS s FROM {table} WHERE id=? AND vm_id=?',(r['operation_id'],vm_id)).fetchone()
                status=task['s'] if task else 'UNKNOWN'
                if task and r['action_kind']=='COMMAND' and status in ('FAILED','SUCCEEDED'):
                    # An older Guest incorrectly treated interactive exit_code=None as
                    # failure even after CreateProcessAsUser succeeded. Accept only an
                    # authenticated Guest report of an actually launched process.
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
                if status!=r['status']:
                    d.execute('UPDATE business_self_executions SET status=?,updated_at=? WHERE request_id=?',(status,utcnow(),r['request_id']))
        return self.executions(service_ref)
