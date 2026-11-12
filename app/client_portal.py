"""Tenant-restricted customer portal and SibHosting business integration.

Admin and client sessions are entirely separate.  Browser clients NEVER receive
VMO_ADMIN_KEY, VMO_BUSINESS_KEY, a VMX path or a Guest Agent credential.

Production deployment: leave the Host Agent bound to loopback and publish ONLY
/client, /client/* through a TLS-authenticated, path-restricted relay.  Trusted
business API calls must use a separate authenticated private channel.
"""
from __future__ import annotations

import hashlib
import json
import logging
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Body, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, ConfigDict

from .fleet import FleetError
from .remote_ops import RemoteStore,RemoteError
from .same_origin_apps import gateway_configured
from .storage import StoreConflict
from .vmrun import VmrunError
from .service_expiry import parse_utc, is_due, ExpiryScheduler
from .self_service import SelfServiceStore, ActionInput, ToggleInput, ExecuteInput
from .customer_settings import CustomerSettings
from .client_output import customer_command_result

REF_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$')
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$')
COOKIE = 'vmo_client_session'
SESSION_LIFETIME = 3 * 3600
TICKET_LIFETIME = 90


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class BusinessDb:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS business_products (
                product_ref TEXT PRIMARY KEY, template_id TEXT NOT NULL,
                allow_updates INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS business_services (
                service_ref TEXT PRIMARY KEY,
                customer_ref TEXT NOT NULL, product_ref TEXT NOT NULL,
                vm_id TEXT NOT NULL UNIQUE, template_id TEXT NOT NULL,
                create_job_id TEXT, status TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS business_tickets (
                ticket_hash TEXT PRIMARY KEY, service_ref TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS business_sessions (
                session_hash TEXT PRIMARY KEY, service_ref TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS business_audit (
                id TEXT PRIMARY KEY, service_ref TEXT NOT NULL,
                action TEXT NOT NULL, result TEXT NOT NULL,
                at INTEGER NOT NULL
            );
            ''')
            # SQLite migration: keep existing customers, tickets and VM bindings.
            cols={r['name'] for r in c.execute('PRAGMA table_info(business_services)')}
            for name, ddl in (
                ('expires_at','TEXT'),('expiry_action',"TEXT DEFAULT 'FORCE_POWER_OFF'"),
                ('expiry_policy_version','INTEGER DEFAULT 1'),
                ('expiry_status','TEXT'),('expiry_generation','INTEGER DEFAULT 0'),
                ('expiry_updated_at','INTEGER'),('expiry_enforced_at','INTEGER'),
                ('expiry_last_error','TEXT'),
                ('expiry_soft_requested_at','INTEGER'),('expiry_soft_generation','INTEGER'),
                ('expiry_hard_attempted_at','INTEGER')):
                if name not in cols:c.execute(f'ALTER TABLE business_services ADD COLUMN {name} {ddl}')

    @contextmanager
    def db(self):
        c = sqlite3.connect(str(self.path), timeout=20)
        c.row_factory = sqlite3.Row
        try:
            c.execute('PRAGMA busy_timeout=20000')
            with c:
                yield c
        finally:
            c.close()

    def audit(self, ref: str, action: str, result: str):
        # Do not record tokens, keys, passwords or full request bodies.
        with self.db() as c:
            c.execute('INSERT INTO business_audit VALUES (?,?,?,?,?)',
                      (str(uuid.uuid4()), ref, action[:60], result[:160], int(time.time())))

    def product(self, ref: str):
        with self.db() as c:
            r = c.execute('SELECT * FROM business_products WHERE product_ref=?', (ref,)).fetchone()
            return dict(r) if r else None

    def products(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM business_products ORDER BY product_ref')]

    def set_product(self, product_ref: str, template_id: str, updates: bool):
        # Existing business_services keep their original template_id; changing the
        # catalog mapping only affects subsequent provisioning.
        with self.lock, self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            before = c.execute('SELECT template_id,allow_updates FROM business_products WHERE product_ref=?',
                               (product_ref,)).fetchone()
            c.execute('INSERT INTO business_products VALUES (?,?,?,?) ON CONFLICT(product_ref) '
                      'DO UPDATE SET template_id=excluded.template_id,allow_updates=excluded.allow_updates',
                      (product_ref, template_id, int(updates), int(time.time())))
            if before is None or before['template_id'] != template_id or bool(before['allow_updates']) != bool(updates):
                detail = ('created template='+template_id if before is None else
                          'old='+before['template_id']+' new='+template_id)
                c.execute('INSERT INTO business_audit VALUES (?,?,?,?,?)',
                          (str(uuid.uuid4()), 'PRODUCT:'+product_ref, 'PRODUCT_MAPPING_SAVED',
                           detail[:160], int(time.time())))
        return self.product(product_ref)

    def delete_product(self, product_ref: str):
        # Unbind the product, NEVER delete the golden-template files or customer VMs.
        # Do not unbind while an order is still being provisioned.
        with self.lock, self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM business_products WHERE product_ref=?',
                            (product_ref,)).fetchone()
            if old is None:
                raise LookupError('Product mapping not found')
            provisioning = c.execute("SELECT COUNT(*) AS n FROM business_services WHERE product_ref=? AND status='PROVISIONING'", (product_ref,)).fetchone()['n']
            if provisioning:
                raise ValueError('Product has provisioning services; wait for them to finish before unbinding')
            existing = c.execute('SELECT COUNT(*) AS n FROM business_services WHERE product_ref=?',
                                 (product_ref,)).fetchone()['n']
            c.execute('DELETE FROM business_products WHERE product_ref=?', (product_ref,))
            c.execute('INSERT INTO business_audit VALUES (?,?,?,?,?)',
                      (str(uuid.uuid4()), 'PRODUCT:'+product_ref, 'PRODUCT_MAPPING_DELETED',
                       ('template='+old['template_id']+' preserved_services='+str(existing))[:160],
                       int(time.time())))
        return {'deleted': True, 'product_ref': product_ref,
                'preserved_services': existing, 'template_deleted': False}

    def service(self, ref: str):
        with self.db() as c:
            r = c.execute('SELECT * FROM business_services WHERE service_ref=?', (ref,)).fetchone()
            return dict(r) if r else None

    def services(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM business_services ORDER BY created_at DESC')]

    def add_service(self, ref: str, owner: str, product: str, vm: str, template: str,
                    job: str | None, expires_at: str | None = None):
        with self.lock, self.db() as c:
            c.execute('''INSERT INTO business_services
                (service_ref,customer_ref,product_ref,vm_id,template_id,create_job_id,status,
                 created_at,expires_at,expiry_action,expiry_policy_version,expiry_status,
                 expiry_generation,expiry_updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (ref,owner,product,vm,template,job,'PROVISIONING' if job else 'ACTIVE',int(time.time()),expires_at,
                 'FORCE_POWER_OFF',1,'SCHEDULED' if expires_at else None,
                 1 if expires_at else 0,int(time.time()) if expires_at else None))
        return self.service(ref)

    @staticmethod
    def expiry_schedule(service):
        if not service or not service.get('expires_at'):return None
        result = {'expires_at':service['expires_at'], 'action':'FORCE_POWER_OFF',
                  'status':service.get('expiry_status') or 'SCHEDULED'}
        requested = service.get('expiry_soft_requested_at')
        if (requested and service.get('expiry_status') != 'ENFORCED' and
                service.get('expiry_soft_generation') == service.get('expiry_generation')):
            from datetime import datetime, timezone
            result['soft_shutdown_at'] = datetime.fromtimestamp(requested,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            result['force_after'] = datetime.fromtimestamp(requested+600,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return result

    def set_expiry(self, ref: str, deadline: str):
        deadline=parse_utc(deadline)
        if is_due(deadline):raise ValueError('Expiry must be in the future')
        with self.lock, self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM business_services WHERE service_ref=?',(ref,)).fetchone()
            if not row:raise LookupError('Service not found')
            if row['status']=='TERMINATED':raise ValueError('Terminated services cannot be renewed')
            previous=row['expires_at']
            if previous==deadline and row['expiry_status']=='SCHEDULED':
                return self.expiry_schedule(dict(row))
            # Explicit manual suspension persists through a renewal. EXPIRED is
            # released for access, but the VMware VM is never auto-powered-on.
            next_state='ACTIVE' if row['status']=='EXPIRED' else row['status']
            c.execute('''UPDATE business_services SET expires_at=?,expiry_action='FORCE_POWER_OFF',
                expiry_policy_version=1,expiry_status='SCHEDULED',
                expiry_generation=COALESCE(expiry_generation,0)+1,expiry_updated_at=?,
                expiry_enforced_at=NULL,expiry_last_error=NULL,
                expiry_soft_requested_at=NULL,expiry_soft_generation=NULL,expiry_hard_attempted_at=NULL,
                status=? WHERE service_ref=?''',
                (deadline,int(time.time()),next_state,ref))
            c.execute('INSERT INTO business_audit VALUES(?,?,?,?,?)',
                (str(uuid.uuid4()),ref,'EXPIRY_CHANGED',
                 ('old='+str(previous)+' new='+deadline)[:160],int(time.time())))
        return self.expiry_schedule(self.service(ref))

    def reconcile_provisioning(self, store):
        # An ACTIVE paid service must not be reported before VMware clone job is done.
        with self.lock:
            for s in self.services():
                if s['status']!='PROVISIONING' or not s['create_job_id']:continue
                job=store.get_job(s['create_job_id'])
                vm=store.get_vm(s['vm_id'])
                target=None
                if job and job['status']=='SUCCEEDED' and vm and vm['state']=='READY':
                    target='ACTIVE'
                elif job and job['status']=='FAILED':
                    target='PROVISION_FAILED'
                if not target:continue
                with self.db() as c:
                    c.execute("UPDATE business_services SET status=? WHERE service_ref=? AND status='PROVISIONING'",
                              (target,s['service_ref']))
                self.audit(s['service_ref'],'PROVISION_STATUS',target)

    def mark_expired(self, ref: str):
        with self.db() as c:
            s=c.execute('SELECT * FROM business_services WHERE service_ref=?',(ref,)).fetchone()
            if not s or not is_due(s['expires_at']) or s['status']=='TERMINATED':return False
            if s['status'] not in ('EXPIRED','SUSPENDED'):
                c.execute('UPDATE business_services SET status=?,expiry_status=? WHERE service_ref=?',
                          ('EXPIRED','ENFORCING',ref))
            if s['expiry_status'] in (None,'SCHEDULED'):
                c.execute('UPDATE business_services SET expiry_status=? WHERE service_ref=?',('ENFORCING',ref))
            c.execute('DELETE FROM business_tickets WHERE service_ref=?',(ref,))
            c.execute('DELETE FROM business_sessions WHERE service_ref=?',(ref,))
            return True

    def mark_expiry_result(self, ref: str, state: str, error: str | None):
        if state not in ('ENFORCING','ENFORCED','FAILED'):raise ValueError('Invalid expiry state')
        with self.db() as c:
            c.execute('UPDATE business_services SET expiry_status=?,expiry_last_error=?,expiry_enforced_at=? WHERE service_ref=?',
                      (state,error,int(time.time()) if state=='ENFORCED' else None,ref))

    def mark_soft_shutdown(self, ref: str, generation: int, requested_at: int):
        # Caller owns self.lock. A stale schedule must never set a new timer.
        with self.db() as c:
            cur=c.execute('''UPDATE business_services
                SET expiry_soft_requested_at=?,expiry_soft_generation=?,expiry_hard_attempted_at=NULL,
                    expiry_status='ENFORCING',expiry_last_error=NULL
                WHERE service_ref=? AND expiry_generation=? AND status IN ('EXPIRED','SUSPENDED')''',
                (requested_at,generation,ref,generation))
            return cur.rowcount == 1

    def mark_hard_shutdown_attempt(self, ref: str, generation: int, attempted_at: int):
        with self.db() as c:
            cur=c.execute('''UPDATE business_services SET expiry_hard_attempted_at=?
                WHERE service_ref=? AND expiry_generation=? AND status IN ('EXPIRED','SUSPENDED')''',
                (attempted_at,ref,generation))
            return cur.rowcount == 1

    def change_status(self, ref: str, state: str):
        with self.lock, self.db() as c:
            old = c.execute('SELECT status FROM business_services WHERE service_ref=?', (ref,)).fetchone()
            if old and old['status'] == 'TERMINATED' and state != 'TERMINATED':
                raise ValueError('Terminated services cannot be reactivated')
            if old and old['status'] in ('PROVISIONING','PROVISION_FAILED') and state=='ACTIVE':
                raise ValueError('Finish or repair provisioning before activation')
            if state=='ACTIVE':
                expiry=c.execute('SELECT expires_at FROM business_services WHERE service_ref=?',(ref,)).fetchone()
                if expiry and is_due(expiry['expires_at']):raise ValueError('Renew expiry before reactivation')
            cur = c.execute('UPDATE business_services SET status=? WHERE service_ref=?', (state, ref))
            if cur.rowcount == 0:
                raise ValueError('Unknown service')
        # Immediately invalidate already issued customer tickets/sessions on suspend/terminate.
        if state != 'ACTIVE':
            with self.db() as c:
                c.execute('DELETE FROM business_tickets WHERE service_ref=?', (ref,))
                c.execute('DELETE FROM business_sessions WHERE service_ref=?', (ref,))
        self.audit(ref, 'SERVICE_STATUS', state)
        return self.service(ref)

    def create_ticket(self, ref: str):
        s = self.service(ref)
        if not s or s['status'] != 'ACTIVE' or is_due(s.get('expires_at')):
            raise ValueError('Service not active')
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.db() as c:
            c.execute('DELETE FROM business_tickets WHERE expires_at<=?', (now,))
            c.execute('INSERT INTO business_tickets VALUES (?,?,?)',
                      (digest(token), ref, now + TICKET_LIFETIME))
        self.audit(ref, 'ISSUE_TICKET', 'issued')
        return token

    def consume_ticket(self, token: str):
        if len(token) > 250 or not token:
            raise ValueError('Invalid ticket')
        now = int(time.time())
        with self.db() as c:
            row = c.execute('SELECT service_ref,expires_at FROM business_tickets WHERE ticket_hash=?',
                            (digest(token),)).fetchone()
            # Consume atomically, including unsuccessful/expired cases.
            if row:
                c.execute('DELETE FROM business_tickets WHERE ticket_hash=?', (digest(token),))
            if not row or row['expires_at'] <= now:
                raise ValueError('Invalid or expired ticket')
            state = c.execute('SELECT status,expires_at FROM business_services WHERE service_ref=?',
                              (row['service_ref'],)).fetchone()
            if not state or state['status'] != 'ACTIVE' or is_due(state['expires_at']):
                raise ValueError('Service disabled')
            session = secrets.token_urlsafe(32)
            c.execute('INSERT INTO business_sessions VALUES (?,?,?)',
                      (digest(session), row['service_ref'], now + SESSION_LIFETIME))
        self.audit(row['service_ref'], 'PORTAL_LOGIN', 'success')
        return session

    def session(self, token: str):
        if not token or len(token) > 250:
            return None
        with self.db() as c:
            row = c.execute('SELECT service_ref,expires_at FROM business_sessions WHERE session_hash=?',
                            (digest(token),)).fetchone()
            if not row or row['expires_at'] <= time.time():
                return None
        s = self.service(row['service_ref'])
        return s if s and s['status'] == 'ACTIVE' and not is_due(s.get('expires_at')) else None

    def logout(self, token: str):
        with self.db() as c:
            c.execute('DELETE FROM business_sessions WHERE session_hash=?', (digest(token),))


class BusinessProduct(BaseModel):
    product_ref: str = Field(min_length=1, max_length=96, pattern=REF_RE.pattern)
    template_id: str
    allow_updates: bool = False


class ExpiryFields(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expires_at: str
    expiry_action: str = 'FORCE_POWER_OFF'
    expiry_policy_version: int = 1

    def verified(self):
        if self.expiry_action != 'FORCE_POWER_OFF' or self.expiry_policy_version != 1:
            raise ValueError('Unsupported expiry policy')
        return parse_utc(self.expires_at)


class CreateService(BaseModel):
    model_config = ConfigDict(extra='forbid')
    service_ref: str = Field(min_length=1, max_length=96, pattern=REF_RE.pattern)
    customer_ref: str = Field(min_length=1, max_length=96, pattern=REF_RE.pattern)
    product_ref: str = Field(min_length=1, max_length=96, pattern=REF_RE.pattern)
    expires_at: str | None = None
    expiry_action: str = 'FORCE_POWER_OFF'
    expiry_policy_version: int = 1


class BindService(CreateService):
    vm_id: str


class TicketRequest(BaseModel):
    customer_ref: str = Field(min_length=1, max_length=96)


class ClientAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str
    snapshot: str | None = None
    package_id: str | None = None
    confirm: bool = False
    confirm_name: str | None = None


def install_client_portal(app, *, settings, store, fleet, vmrun, runner, templates_root,
                          vms_root, authorized, vm_detail, running_set, managed_vm,
                          clone_full, queue_vm_action, screenshot, create_snapshot,
                          restore_snapshot, delete_snapshot, reinstall_vm, software_update):
    """Register client/business/admin routes into the existing FastAPI instance."""
    from .main import (CloneRequest, SnapshotCreate, ConfirmRestore, SnapshotDeleteRequest,
                       ReinstallVmRequest, UpdateRequest)

    db = BusinessDb(settings.db_path)
    remote = RemoteStore(settings.db_path)
    self_actions = SelfServiceStore(settings.db_path,remote_store=remote)
    customer_settings = CustomerSettings(settings.db_path)
    portal_dir = Path(__file__).parent / 'ui'
    business_key = os.environ.get('VMO_BUSINESS_KEY', '')
    from .admin_auth import AdminAuth
    keys=AdminAuth(settings.db_path,settings.admin_key,business_key)
    scheduler=ExpiryScheduler(db,store,vmrun,vms_root,remote)
    @app.on_event('startup')
    def start_expiry_scheduler(): scheduler.start()
    @app.on_event('shutdown')
    def stop_expiry_scheduler(): scheduler.stop()

    def expiry_host_ready():
        # Operator choice: no Windows autostart or separate scheduled task.
        # The expiry worker lives inside the current 8765 Host process only.
        # When 8765 exits it cannot enforce deadlines until restarted.
        return scheduler.thread is not None and scheduler.thread.is_alive()

    def require_expiry_host():
        if not expiry_host_ready():
            raise HTTPException(503,'VMO Host expiry worker is not running')

    def check_expiry_fields(payload):
        if payload.expiry_action!='FORCE_POWER_OFF' or payload.expiry_policy_version!=1:
            raise HTTPException(422,'Only FORCE_POWER_OFF policy version 1 supported')
        if payload.expires_at is None:return None
        try:
            deadline=parse_utc(payload.expires_at)
        except ValueError as exc:raise HTTPException(422,str(exc))
        if is_due(deadline):raise HTTPException(422,'Expiry cannot be in the past')
        return deadline

    def validate_product(ref):
        p = db.product(ref)
        t = store.get(p['template_id']) if p else None
        if not p or not t or t['status'] != 'SEALED_ATTESTED':
            raise HTTPException(409, 'Product must map to a sealed golden template')
        return p, t

    def trusted_business(x_vmo_business_key: str | None = Header(default=None)):
        if not keys.check_api(x_vmo_business_key,'business'):
            raise HTTPException(401,'Invalid or revoked business API key')

    def client_service(request: Request, x_client_csrf: str | None = Header(default=None)):
        token = request.cookies.get(COOKIE, '')
        s = db.session(token)
        if not s:
            raise HTTPException(401, 'Client session expired; reopen from SibHosting')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            expected = hmac.new(business_key.encode(), ('csrf:'+token).encode(), hashlib.sha256).hexdigest() if business_key else ''
            if not x_client_csrf or not hmac.compare_digest(x_client_csrf, expected):
                raise HTTPException(403, 'Client CSRF check failed')
        return s

    def get_owned_vm(s):
        v = store.get_vm(s['vm_id'])
        if not v or v['state'] == 'DELETED':
            raise HTTPException(409, 'Service VM not available')
        return v

    def reinstall_source(s):
        """Resolve only a template already assigned to this service/product.

        An existing service keeps its original template when it is still usable.
        If that original was archived, removed or lost from disk, the only
        fallback is the *current mapping of the same product*. Never use the
        global default template or a template supplied by the browser.
        """
        original = store.get(s['template_id'])
        if (original and original['status'] == 'SEALED_ATTESTED'
                and Path(original['vmx_path']).is_file()):
            return original, False
        product = db.product(s['product_ref'])
        replacement = store.get(product['template_id']) if product else None
        if (replacement and replacement['status'] == 'SEALED_ATTESTED'
                and Path(replacement['vmx_path']).is_file()):
            return replacement, replacement['id'] != s['template_id']
        raise HTTPException(409, '当前套餐的系统模板已失效，请联系管理员在「商品与模板」中重新绑定有效且已封存的黄金模板')

    def safe_vm_status(s):
        v = get_owned_vm(s)
        data = vm_detail(v, running_set())
        g = next((g for g in fleet.guests() if g['vm_id'] == v['id'] and not g.get('revoked_at')), None)
        jobs = store.list_jobs(v['id'])
        try:
            reinstall_source(s)
            can_reinstall = True
        except HTTPException:
            can_reinstall = False
        return {
            'service_ref': s['service_ref'], 'name': v['name'],
            'status': s['status'], 'expires_at':s.get('expires_at'),
            'expiry_schedule':db.expiry_schedule(s),
            'vm_state': v['state'], 'power_state': data['power_state'],
            'guest_registered': bool(g), 'guest_last_seen': g['last_seen'] if g else None,
            'guest_ipv4_addresses': (g.get('reported') or {}).get('ipv4_addresses', []) if g else [],
            'guest_install_modes': (g.get('reported') or {}).get('install_modes', {}) if g else {},
            'latest_job': ({k: jobs[0].get(k) for k in ('id','action','status')} if jobs else None),
            'allow_updates': bool((db.product(s['product_ref']) or {}).get('allow_updates')),
            'reinstall_template_available': can_reinstall,
        }

    @app.get('/client', include_in_schema=False)
    def client_home():
        return FileResponse(portal_dir / 'client.html', media_type='text/html; charset=utf-8',
                            headers={'Cache-Control':'no-store'})

    @app.get('/ui/client.css', include_in_schema=False)
    def client_css():
        return FileResponse(portal_dir / 'client.css', media_type='text/css; charset=utf-8', headers={'Cache-Control':'no-store'})

    @app.get('/ui/client.js', include_in_schema=False)
    def client_js():
        return FileResponse(portal_dir / 'client.js', media_type='text/javascript; charset=utf-8', headers={'Cache-Control':'no-store'})


    @app.post('/client/exchange', include_in_schema=False)
    async def exchange(request: Request):
        # POST form ticket: never place a login ticket in URL, logs or Referer.
        raw = await request.body()
        if len(raw) > 4096:
            raise HTTPException(413, 'Request too large')
        # Preview and SibHosting both send application/x-www-form-urlencoded.
        # Reject malformed input without reaching SQLite or logging the token.
        if not request.headers.get('content-type', '').split(';')[0].strip().lower() == 'application/x-www-form-urlencoded':
            raise HTTPException(415, 'Use a form POST to exchange client tickets')
        form = parse_qs(raw.decode('ascii', errors='ignore'), keep_blank_values=True)
        values = form.get('ticket', [])
        if len(values) != 1 or not values[0]:
            raise HTTPException(400, 'Exactly one nonempty ticket is required')
        try:
            session = db.consume_ticket(values[0])
        except ValueError:
            raise HTTPException(401, 'Client login token expired or already used')
        except sqlite3.Error:
            # Provide a controlled retryable status rather than exposing a raw
            # Internal Server Error; the full traceback stays in Host logs.
            logging.getLogger('vmo.client_exchange').exception('Ticket exchange database failure')
            raise HTTPException(503, 'Client ticket database temporarily unavailable; check VMO Host logs')
        except Exception:
            logging.getLogger('vmo.client_exchange').exception('Unexpected ticket exchange failure')
            raise HTTPException(503, 'Client preview exchange failed; check VMO Host logs')
        r = RedirectResponse('/client', status_code=303)
        embedded = bool(os.environ.get('VMO_PUBLIC_BUSINESS_ORIGIN',''))
        r.set_cookie(COOKIE, session, max_age=SESSION_LIFETIME, httponly=True,
                     secure=(embedded or request.url.scheme == 'https' or os.environ.get('VMO_CLIENT_SECURE_COOKIE') == '1'),
                     samesite='none' if embedded else 'lax', path='/client')
        return r

    @app.get('/client/api/me', include_in_schema=False)
    def client_me(request: Request, s=Depends(client_service)):
        token = request.cookies.get(COOKIE, '')
        return {**safe_vm_status(s), 'limits':customer_settings.limits(s['service_ref']), 'usage':customer_settings.usage(s['service_ref']), 'csrf': hmac.new(business_key.encode(), ('csrf:'+token).encode(), hashlib.sha256).hexdigest()}

    @app.post('/client/api/logout', include_in_schema=False)
    def client_logout(request: Request, s=Depends(client_service)):
        remote.revoke_app_sessions(s['service_ref'])
        db.logout(request.cookies.get(COOKIE, ''))
        r = Response(status_code=204)
        r.delete_cookie(COOKIE, path='/client')
        return r

    @app.get('/client/api/apps', include_in_schema=False)
    def client_apps(s=Depends(client_service)):
        # The browser receives no internal loopback URLs, IPs, tokens or VM IDs.
        wss_online=remote.link_presence(s['vm_id']).get('state')=='connected'
        return [{'app_id':a['app_id'],'version':a['version'],
                 'auth_state':a['auth_state'],'online':bool(a.get('is_available')) and wss_online,
                 'last_report':a.get('reported_at'),
                 'secret_reported_at':a.get('secret_reported_at'),
                 'last_seen':a['updated_at'],
                 'web_access':('https_wss_relay' if a.get('is_available') and wss_online else 'offline') if gateway_configured() else 'unconfigured'} for a in remote.apps(s['vm_id'])]

    @app.post('/client/api/apps/{app_id}/launch', include_in_schema=False)
    def client_app_launch(app_id:str,s=Depends(client_service)):
        if app_id not in ('snowluma','dsh-fish-soul'):
            raise HTTPException(404,'Application not supported')
        if not gateway_configured():
            raise HTTPException(503,'8766 应用网关未就绪，请检查宿主机网关服务')
        # Only an ACTIVE business-service owner may request a 60s one-use ticket.
        try: ticket=remote.issue_app_launch(s['service_ref'],s['vm_id'],app_id)
        except RemoteError as exc:raise HTTPException(409,str(exc))
        db.audit(s['service_ref'],'APP_LAUNCH',app_id)
        return {'gateway':'/apps/open','ticket':ticket,'expires_in':60}

    @app.post('/client/api/apps/{app_id}/reveal' , include_in_schema=False)
    def client_app_access_code(app_id: str, payload: dict, s=Depends(client_service)):
        if payload.get('confirm') is not True:
            raise HTTPException(422,'Confirm that the access code is sensitive')
        try: secret=remote.app_secret(s['vm_id'],app_id)
        except (RemoteError,RuntimeError):raise HTTPException(409,'App access code unavailable')
        if secret is None:raise HTTPException(404,'Access code not yet reported')
        db.audit(s['service_ref'],'APP_ACCESS_CODE_REVEAL',app_id)
        reported=next((a.get('secret_reported_at') for a in remote.apps(s['vm_id']) if a['app_id']==app_id),None)
        return Response(content=json.dumps({'access_code':secret,'reported_at':reported}),media_type='application/json',
                        headers={'Cache-Control':'no-store'})

    @app.get('/client/api/jobs', include_in_schema=False)
    def client_jobs(s=Depends(client_service)):
        # No VMX path, template ID or other customers' job metadata.
        return [{k: j.get(k) for k in ('id', 'action', 'status', 'created_at', 'completed_at')}
                for j in store.list_jobs(s['vm_id'],limit=5)]

    @app.get('/client/api/snapshots', include_in_schema=False)
    def client_snapshots(s=Depends(client_service)):
        v, vmx = managed_vm(s['vm_id'])
        if v['state'] != 'READY':
            return {'snapshots': [], 'count': 0, 'limit': customer_settings.limits(s['service_ref'])['snapshot_limit'], 'can_create': False}
        try:
            snaps = vmrun.snapshots(vmx)
            limit=customer_settings.limits(s['service_ref'])['snapshot_limit']
            return {'snapshots': snaps, 'count': len(snaps), 'limit': limit,
                    'can_create': len(snaps) < limit and vm_detail(v, running_set())['power_state'] == 'POWERED_OFF'}
        except VmrunError:
            raise HTTPException(503, 'Snapshot status unavailable')

    @app.get('/client/api/self-actions', include_in_schema=False)
    def client_self_actions(s=Depends(client_service)):
        get_owned_vm(s)
        product=db.product(s['product_ref']) or {}
        result=[]
        for a in self_actions.list(s['product_ref'],enabled_only=True):
            if a['kind']=='SOFTWARE_UPDATE' and not product.get('allow_updates'):
                continue
            result.append(self_actions.public(a))
        return result

    @app.get('/client/api/self-actions/history', include_in_schema=False)
    def client_self_action_history(s=Depends(client_service)):
        records=self_actions.reconcile(s['service_ref'],s['vm_id'])
        # Return bounded, redacted command output for the tenant's own VM only.
        history=[]
        for row in records:
            visible={k:row.get(k) for k in ('request_id','action_id','status','created_at','updated_at')}
            # Never expose the actual command, directory, tokens or package path.
            action=self_actions.get(row['action_id']) if not row.get('action_label') else None
            visible['label']=row.get('action_label') or (action or {}).get('label') or '已删除的自助操作' 
            if row['action_kind']=='COMMAND' and row.get('operation_id'):
                # Only this authenticated tenant's VM; never return source command,
                # full result metadata, internal paths or Agent credentials.
                op=remote.get(row['operation_id'])
                if op and op.get('vm_id')==s['vm_id']:
                    visible.update(customer_command_result(op.get('result'),visible['status']))
            history.append(visible)
        blocking=self_actions.blocking_execution(s['service_ref'],s['vm_id'])
        # The blocker is independent from the five most recent entries.
        return {'items':history,'blocker':({
            'status':blocking['status'],
            'label':blocking.get('action_label') or '之前的自助操作'
        } if blocking else None)}

    @app.post('/client/api/self-actions/{action_id}/execute', include_in_schema=False,status_code=202)
    def client_execute_self_action(action_id:str,payload:ExecuteInput,s=Depends(client_service)):
        if not payload.confirm:raise HTTPException(422,'Please confirm the customer operation')
        v=get_owned_vm(s)
        if v['state']!='READY' or vm_detail(v,running_set()).get('power_state')!='POWERED_ON':
            raise HTTPException(409,'Windows VM must be running and ready')
        a=self_actions.get(action_id)
        if not a or not a['enabled'] or a['product_ref']!=s['product_ref']:
            raise HTTPException(404,'Self-service operation unavailable for this product')
        if a['kind']=='SOFTWARE_UPDATE' and not (db.product(s['product_ref']) or {}).get('allow_updates'):
            raise HTTPException(403,'This product does not permit software updates')
        if remote.link_presence(v['id']).get('state')!='connected':
            raise HTTPException(409,'Guest WSS offline; operation cannot be dispatched')
        if not any(g['vm_id']==v['id'] and not g.get('revoked_at') for g in fleet.guests()):
            raise HTTPException(409,'Guest Agent is not enrolled')
        with self_actions.lock:
            # Reconcile durable results before checking duplicate execution.
            self_actions.reconcile(s['service_ref'],v['id'])
            try:record,created=self_actions.reserve(s['service_ref'],v['id'],a['id'],a['kind'],payload.request_id)
            except LookupError as exc:raise HTTPException(404,str(exc))
            except (ValueError,RuntimeError) as exc:raise HTTPException(409,str(exc))
            if not created:
                return {'request_id':record['request_id'],'status':record['status'],
                        'note':'Same request already accepted; it will not be executed again'}
            try:
                if a['kind']=='COMMAND':
                    op=remote.queue(v['id'],'COMMAND',{
                        'shell':a['shell'],'command':a['command'],'privilege':'system',
                        'confirmed':True,'run_mode':a['run_mode'],'cwd':a['cwd'],
                        'timeout_seconds':a['timeout_seconds']},ttl=420)
                else:
                    result=software_update(v['id'],UpdateRequest(package_id=a['package_id'],
                        install_dir=a['install_dir'] or None,
                        confirm_backup_and_compatibility=True))
                    op=result['task_id']
            except (FleetError,RemoteError,VmrunError,StoreConflict,ValueError,HTTPException) as exc:
                self_actions.record(payload.request_id,'FAILED',error=type(exc).__name__)
                db.audit(s['service_ref'],'SELF_ACTION_REJECTED',a['kind'])
                if isinstance(exc,HTTPException):raise
                raise HTTPException(409,'Operation could not be queued; check the software adapter and Guest status') from exc
            self_actions.record(payload.request_id,'QUEUED',op)
        db.audit(s['service_ref'],'SELF_ACTION_QUEUED',a['kind']+':'+a['id'])
        return {'request_id':str(payload.request_id),'status':'QUEUED',
                'note':'Task accepted. Running status and final outcome will appear below.'}

    @app.get('/client/api/announcement', include_in_schema=False)
    def client_announcement(s=Depends(client_service)):
        return {'announcement':customer_settings.pending(s['service_ref'])}

    @app.post('/client/api/announcement/{announcement_id}/ack', include_in_schema=False)
    def client_announcement_ack(announcement_id:str,s=Depends(client_service)):
        try:return customer_settings.acknowledge(s['service_ref'],announcement_id)
        except ValueError as exc:raise HTTPException(409,str(exc))

    @app.get('/client/api/packages', include_in_schema=False)
    def client_packages(s=Depends(client_service)):
        # Deprecated: update package selection now belongs to administrator catalog.
        return []

    @app.get('/client/api/screen.png', include_in_schema=False)
    def client_screen(s=Depends(client_service)):
        # Reuse Host Agent's local VNC capture with the already verified VM ID.
        return screenshot(s['vm_id'])

    @app.post('/client/api/action', include_in_schema=False, status_code=202)
    def client_action(payload: ClientAction, s=Depends(client_service)):
        v = get_owned_vm(s)
        vm_id = v['id']
        _verified_vm, vmx = managed_vm(vm_id)
        permitted = {'start', 'stop', 'hard_stop', 'snapshot_create',
                     'snapshot_restore', 'snapshot_delete', 'reinstall'}
        if payload.action not in permitted:
            raise HTTPException(422, 'Operation not permitted')
        try:
            if payload.action == 'start':
                result = queue_vm_action(vm_id, 'START')
            elif payload.action == 'stop':
                result = queue_vm_action(vm_id, 'STOP', mode='soft')
            elif payload.action == 'hard_stop':
                if not payload.confirm or payload.confirm_name != v['name']:
                    raise HTTPException(422, 'Hard stop requires exact VM name confirmation')
                result = queue_vm_action(vm_id, 'STOP', mode='hard')
            elif payload.action == 'snapshot_create':
                try:
                    if len(vmrun.snapshots(vmx)) >= customer_settings.limits(s['service_ref'])['snapshot_limit']:
                        raise HTTPException(409,'快照数量已达上限')
                except VmrunError:raise HTTPException(503,'无法读取快照数量')
                if not payload.snapshot or not NAME_RE.fullmatch(payload.snapshot):
                    raise HTTPException(422, 'Invalid snapshot name')
                result = create_snapshot(vm_id, SnapshotCreate(name=payload.snapshot))
            elif payload.action in ('snapshot_restore', 'snapshot_delete'):
                if not payload.snapshot or not NAME_RE.fullmatch(payload.snapshot):
                    raise HTTPException(422, 'Invalid snapshot name')
                if not payload.confirm or payload.confirm_name != v['name']:
                    raise HTTPException(422, 'Snapshot change requires exact VM name confirmation')
                if payload.action == 'snapshot_restore':
                    try:quota_month=customer_settings.reserve(s['service_ref'],'snapshot_restore')
                    except ValueError as exc:raise HTTPException(429,str(exc))
                    try:result = restore_snapshot(vm_id, payload.snapshot, ConfirmRestore(confirm_data_loss=True))
                    except Exception:
                        customer_settings.release(s['service_ref'],'snapshot_restore',quota_month)
                        raise
                else:
                    result = delete_snapshot(vm_id, payload.snapshot, SnapshotDeleteRequest())
            elif payload.action == 'reinstall':
                if not payload.confirm or payload.confirm_name != v['name']:
                    raise HTTPException(422, 'Reinstall requires exact VM name confirmation')
                t, remapped = reinstall_source(s)
                try:quota_month=customer_settings.reserve(s['service_ref'],'reinstall')
                except ValueError as exc:raise HTTPException(429,str(exc))
                try:result = reinstall_vm(vm_id, ReinstallVmRequest(
                    template_id=t['id'], unattended_oobe=t.get('oobe_bootstrap_version',0)>=2,
                    enable_screen=True, confirm_name=v['name'],
                    confirm_erase_customer_data=True, confirm_backup_disk_space=True))
                except Exception:
                    customer_settings.release(s['service_ref'],'reinstall',quota_month)
                    raise
                if remapped:
                    # Preserve original service/template history. Record that
                    # the currently approved product mapping was used instead.
                    db.audit(s['service_ref'], 'REINSTALL_TEMPLATE_FALLBACK',
                             'product='+s['product_ref']+' template='+t['id'])
            else:  # software_update
                p = db.product(s['product_ref'])
                if not p or not p['allow_updates']:
                    raise HTTPException(403, 'Software updates not included in this product')
                if not payload.confirm or payload.confirm_name != v['name']:
                    raise HTTPException(422, 'Software update requires exact VM name confirmation')
                if not payload.package_id or not fleet.package(payload.package_id):
                    raise HTTPException(404, 'Approved software package not found')
                if not any(g['vm_id'] == vm_id and not g.get('revoked_at') for g in fleet.guests()):
                    raise HTTPException(409, 'Guest Agent must register before software updates')
                result = software_update(vm_id, UpdateRequest(
                    package_id=payload.package_id, confirm_backup_and_compatibility=True))
        except (StoreConflict, FleetError, VmrunError) as exc:
            db.audit(s['service_ref'], payload.action, 'failed')
            raise HTTPException(409, str(exc))
        db.audit(s['service_ref'], payload.action, 'accepted')
        return {'status':'QUEUED', **result}

    # Trusted business connector: NEVER exposed through client session.
    @app.get('/api/business/health', dependencies=[Depends(trusted_business)])
    def business_health():
        return {'status':'ok', 'client_portal':'integrated', 'admin_api_public':False,
                'capabilities':['service_expiry_v1'] if expiry_host_ready() else [],
                'expiry_worker_mode':'host_process_only'}

    @app.get('/api/business/services/{service_ref}', dependencies=[Depends(trusted_business)])
    def business_service_status(service_ref: str):
        s = db.service(service_ref)
        if not s:
            raise HTTPException(404, 'Unknown business instance')
        v = store.get_vm(s['vm_id'])
        state = v['state'] if v else 'NOT_FOUND'
        return {'service_ref':s['service_ref'], 'status':s['status'],
                'vm_state':state, 'expiry_schedule':db.expiry_schedule(s), 'provision_job_status':
                (store.get_job(s['create_job_id']) or {}).get('status') if s['create_job_id'] else None}

    @app.get('/api/business/services', dependencies=[Depends(trusted_business)])
    def business_services():
        return db.services()

    @app.post('/api/business/services', dependencies=[Depends(trusted_business)], status_code=202)
    def business_create(payload: CreateService):
        deadline=check_expiry_fields(payload)
        if deadline:require_expiry_host()
        if not deadline and os.environ.get('VMO_REQUIRE_SERVICE_EXPIRY','0')=='1':
            raise HTTPException(422,'Paid instance expiry required')
        with db.lock:
            existing = db.service(payload.service_ref)
            if existing:
                if existing['customer_ref'] != payload.customer_ref or existing['product_ref'] != payload.product_ref:
                    raise HTTPException(409, 'Service already assigned to different customer or product')
                if deadline != existing.get('expires_at'):
                    raise HTTPException(409,'Expiry mismatch: use PUT /expiry to renew an existing service')
                return {'service_ref': payload.service_ref,
                        'status':existing['status'], 'created':False,
                        'expiry_schedule':db.expiry_schedule(existing)}
            p,t = validate_product(payload.product_ref)
            # Deterministic name; no arbitrary path/VMX/VM id from business caller.
            name = 'SIB-' + hashlib.sha256(payload.service_ref.encode()).hexdigest()[:14]
            # Recovery for process crash between VMware clone reservation and
            # business association: reattach the previously reserved VM rather
            # than enqueue a duplicate paid instance.
            reserved = next((v for v in store.list_vms() if
                             v['name'] == name and v['customer_ref'] == 'sib:'+payload.service_ref), None)
            if reserved:
                clone_job = next((j for j in store.list_jobs(reserved['id'])
                                  if j['action'] == 'CLONE_FULL'), None)
                vm_id = reserved['id']
                job_id = clone_job['id'] if clone_job else None
            else:
                job = clone_full(CloneRequest(name=name, template_id=t['id'],
                    customer_ref='sib:'+payload.service_ref,
                    start_after_clone=True, enable_screen=True,
                    unattended_oobe=t.get('oobe_bootstrap_version',0)>=2))
                vm_id, job_id = job['vm_id'], job['job_id']
            try:
                db.add_service(payload.service_ref, payload.customer_ref, payload.product_ref,
                               vm_id, t['id'], job_id, deadline)
            except sqlite3.IntegrityError:
                raise HTTPException(409, 'Service already exists; verify provisioning state')
            db.audit(payload.service_ref, 'PROVISION', 'accepted')
            return {'service_ref':payload.service_ref, 'status':'PROVISIONING', 'created':True,
                    'expiry_schedule':db.expiry_schedule(db.service(payload.service_ref))}

    @app.put('/api/business/services/{service_ref}/expiry', dependencies=[Depends(trusted_business)])
    def business_update_expiry(service_ref: str, payload: ExpiryFields):
        require_expiry_host()
        try:deadline=payload.verified();schedule=db.set_expiry(service_ref,deadline)
        except LookupError:raise HTTPException(404,'Service not found')
        except ValueError as exc:raise HTTPException(422,str(exc))
        return {'expiry_schedule':schedule}

    @app.put('/api/business/admin/services/{service_ref}/expiry', dependencies=[Depends(authorized)])
    def admin_update_expiry(service_ref: str,payload: ExpiryFields):
        require_expiry_host()
        try:return {'expiry_schedule':db.set_expiry(service_ref,payload.verified())}
        except LookupError:raise HTTPException(404,'Service not found')
        except ValueError as exc:raise HTTPException(422,str(exc))

    @app.post('/api/business/services/{service_ref}/state', dependencies=[Depends(trusted_business)])
    def business_change_state(service_ref: str, status: str = Body(embed=True)):
        if status not in ('ACTIVE','SUSPENDED','TERMINATED'):
            raise HTTPException(422, 'Invalid service status')
        try:
            s = db.change_status(service_ref, status)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        if status!='ACTIVE':remote.revoke_app_sessions(service_ref)
        # Scheduler enforces suspension power-off independently of business cron.
        return {'service_ref':s['service_ref'], 'status':s['status'],
                'note':'Suspended services are forced off by the host scheduler; VM disks are preserved'}

    @app.post('/api/business/services/{service_ref}/ticket', dependencies=[Depends(trusted_business)])
    def business_ticket(service_ref: str, payload: TicketRequest):
        s = db.service(service_ref)
        if not s or s['customer_ref'] != payload.customer_ref or s['status'] != 'ACTIVE' or is_due(s.get('expires_at')):
            raise HTTPException(404, 'Service not available to this customer')
        token = db.create_ticket(service_ref)
        return {'ticket':token, 'expires_in':TICKET_LIFETIME,
                'method':'POST', 'action':'/client/exchange'}

    # Admin-only module under the SAME existing console.
    @app.get('/api/business/admin/self-executions', dependencies=[Depends(authorized)])
    def admin_self_executions():
        return self_actions.admin_executions()

    @app.post('/api/business/admin/self-executions/{request_id}/acknowledge',dependencies=[Depends(authorized)])
    def admin_acknowledge_execution(request_id:str,payload:dict):
        if payload.get('reviewed_guest_state') is not True:
            raise HTTPException(422,'Confirm that Guest process and logs have been inspected')
        try:
            with self_actions.db() as connection:
                entry=connection.execute('SELECT service_ref,vm_id FROM business_self_executions WHERE request_id=?',(request_id,)).fetchone()
            if not entry:raise LookupError('Execution record not found')
            self_actions.reconcile(entry['service_ref'],entry['vm_id'])
            original=self_actions.acknowledge(request_id)
        except LookupError:raise HTTPException(404,'Execution record not found')
        except ValueError as exc:raise HTTPException(409,str(exc))
        db.audit(original['service_ref'],'SELF_ACTION_MANUAL_REVIEW',original['action_kind'])
        return {'status':'ACKNOWLEDGED','note':'Review recorded; no customer-operation restriction was applied'}


    def remote_self_service_for_vm(vm_id: str):
        """Resolve customer operations by a server-side VM binding, never by client input."""
        vm=store.get_vm(vm_id)
        if not vm or vm['state']!='READY':
            raise HTTPException(404,'Managed VM not ready')
        with db.db() as connection:
            row=connection.execute(
                'SELECT * FROM business_services WHERE vm_id=? LIMIT 1',(vm_id,)
            ).fetchone()
        return dict(row) if row else None

    @app.get('/api/vms/{vm_id}/remote/self-actions', dependencies=[Depends(authorized)])
    def admin_remote_self_actions(vm_id: str):
        """Reuse customer-visible preset actions in the remote-admin page.

        Commands/working directories are intentionally never sent to the
        embedded widget. Admin sees only labels, descriptions and results.
        """
        service=remote_self_service_for_vm(vm_id)
        if not service:
            return {'items':[],'executions':[],
                    'message':'该虚拟机尚未关联思博客户服务，因此没有可用的客户自助操作'}
        if service['status']!='ACTIVE' or (service.get('expires_at') and
              is_due(parse_utc(service['expires_at']))):
            return {'items':[],'executions':[],
                    'message':'客户服务已暂停或到期；不能从远程运维触发其自助操作'}
        actions=[self_actions.public(a) for a in
                 self_actions.list(service['product_ref'], enabled_only=True)]
        rows=self_actions.reconcile(service['service_ref'],vm_id)
        history=[]
        for row in rows:
            visible={k:row.get(k) for k in (
                'request_id','action_id','action_kind','operation_id',
                'status','created_at','updated_at')}
            visible['label']=row.get('action_label') or '客户自助操作'
            if row['action_kind']=='SOFTWARE_UPDATE' and row.get('operation_id'):
                with self_actions.db() as connection:
                    task=connection.execute(
                        'SELECT status,message FROM guest_tasks WHERE id=? AND vm_id=?',
                        (row['operation_id'],vm_id)).fetchone()
                if task:
                    visible['output_text']=str(task['message'] or '')[:800]
            history.append(visible)
        return {'items':actions,'executions':history,
                'message':('可执行 '+str(len(actions))+' 项客户自助操作'
                           if actions else '该客户套餐目前没有已启用的自助操作')}

    @app.post('/api/vms/{vm_id}/remote/self-actions/{action_id}/execute',
              dependencies=[Depends(authorized)],status_code=202)
    def admin_remote_execute_self_action(vm_id: str,action_id: str,payload: ExecuteInput):
        """Admin trigger uses exactly the same audited, idempotent customer path."""
        service=remote_self_service_for_vm(vm_id)
        if not service:
            raise HTTPException(404,'该虚拟机未关联客户服务')
        if service['status']!='ACTIVE' or (service.get('expires_at') and
                is_due(parse_utc(service['expires_at']))):
            raise HTTPException(403,'客户服务已暂停或到期，不允许执行')
        result=client_execute_self_action(action_id,payload,service)
        db.audit(service['service_ref'],'ADMIN_REMOTE_SELF_ACTION',
                 action_id[:80])
        return result

    @app.get('/api/business/admin/self-actions', dependencies=[Depends(authorized)])
    def admin_self_actions():
        return self_actions.list()

    @app.post('/api/business/admin/self-actions', dependencies=[Depends(authorized)])
    def admin_save_self_action(payload:ActionInput):
        if not db.product(payload.product_ref):raise HTTPException(409,'Configure the product before assigning operations')
        try:
            if payload.kind=='COMMAND' and payload.cwd:
                from .remote_ops import validate_command_directory
                validate_command_directory(payload.cwd)
            if payload.kind=='SOFTWARE_UPDATE':
                package=fleet.package(payload.package_id)
                if not package:raise ValueError('Approved update package not found')
                if payload.install_dir:
                    from .update_commands import validate_install_dir
                    validate_install_dir(payload.install_dir)
                    if package['product']!='snowluma':
                        raise ValueError('Install directory override only supports SnowLuma')
            return self_actions.save(payload)
        except LookupError:raise HTTPException(404,'Operation not found')
        except (ValueError,RemoteError) as exc:raise HTTPException(422,str(exc))

    @app.put('/api/business/admin/self-actions/{action_id}/enabled', dependencies=[Depends(authorized)])
    def admin_toggle_self_action(action_id:str,payload:ToggleInput):
        try:return self_actions.toggle(action_id,payload.enabled)
        except LookupError:raise HTTPException(404,'Operation not found')

    @app.delete('/api/business/admin/self-actions/{action_id}', dependencies=[Depends(authorized)])
    def admin_delete_self_action(action_id:str):
        try:self_actions.delete(action_id)
        except LookupError:raise HTTPException(404,'Operation not found')
        return {'deleted':True}

    @app.get('/api/business/admin/customer-settings', dependencies=[Depends(authorized)])
    def admin_customer_settings():
        return {'defaults':customer_settings.defaults(),'announcement':customer_settings.active_announcement()}

    @app.put('/api/business/admin/customer-settings/defaults', dependencies=[Depends(authorized)])
    def admin_customer_defaults(payload:dict):
        try:return customer_settings.set_limits(payload)
        except ValueError as exc:raise HTTPException(422,str(exc))

    @app.get('/api/business/admin/customer-settings/services/{service_ref}', dependencies=[Depends(authorized)])
    def admin_customer_limits(service_ref:str):
        if not db.service(service_ref):raise HTTPException(404,'客户服务不存在')
        return {'limits':customer_settings.limits(service_ref),'usage':customer_settings.usage(service_ref)}

    @app.put('/api/business/admin/customer-settings/services/{service_ref}', dependencies=[Depends(authorized)])
    def admin_set_customer_limits(service_ref:str,payload:dict):
        if not db.service(service_ref):raise HTTPException(404,'客户服务不存在')
        try:return customer_settings.set_limits(payload,service_ref)
        except ValueError as exc:raise HTTPException(422,str(exc))

    @app.post('/api/business/admin/customer-announcement', dependencies=[Depends(authorized)])
    def admin_publish_announcement(payload:dict):
        if set(payload)!={'body','repeat_hours','enabled'}:raise HTTPException(422,'公告参数不正确')
        try:return customer_settings.publish(payload['body'],payload['repeat_hours'],payload['enabled'])
        except (ValueError,TypeError,KeyError) as exc:raise HTTPException(422,str(exc))

    @app.get('/api/business/admin/products', dependencies=[Depends(authorized)])
    def admin_business_products():
        return db.products()

    @app.put('/api/business/admin/products', dependencies=[Depends(authorized)])
    def admin_set_business_product(payload: BusinessProduct):
        validate = store.get(payload.template_id)
        if not validate or validate['status'] != 'SEALED_ATTESTED':
            raise HTTPException(409, 'Choose a sealed golden template')
        return db.set_product(payload.product_ref, payload.template_id, payload.allow_updates)

    @app.delete('/api/business/admin/products/{product_ref}', dependencies=[Depends(authorized)])
    def admin_delete_business_product(product_ref: str):
        if not REF_RE.fullmatch(product_ref):
            raise HTTPException(422, 'Invalid product reference')
        try:
            return db.delete_product(product_ref)
        except LookupError:
            raise HTTPException(404, 'Product mapping not found')
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.get('/api/business/admin/services', dependencies=[Depends(authorized)])
    def admin_business_services():
        return db.services()

    @app.post('/api/business/admin/bind-existing', dependencies=[Depends(authorized)], status_code=201)
    def admin_bind_service(payload: BindService):
        # Test/migration path: bind only an existing managed ready VM, atomically.
        v = store.get_vm(payload.vm_id)
        p = db.product(payload.product_ref)
        if not v or v['state'] != 'READY' or not p:
            raise HTTPException(409, 'Choose an existing READY VM and configured product')
        deadline=check_expiry_fields(payload)
        if not deadline:raise HTTPException(422,'Expiry required when manually binding a customer')
        require_expiry_host()
        try:
            result = db.add_service(payload.service_ref, payload.customer_ref,
                                    payload.product_ref, payload.vm_id, p['template_id'], None, deadline)
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'VM or service already assigned')
        db.audit(payload.service_ref, 'MANUAL_BIND', 'created')
        return result

    @app.post('/api/business/admin/services/{service_ref}/test-ticket', dependencies=[Depends(authorized)])
    def admin_test_ticket(service_ref: str):
        try:
            token = db.create_ticket(service_ref)
        except ValueError:
            raise HTTPException(409, 'Service not active')
        return {'ticket':token, 'method':'POST', 'action':'/client/exchange', 'expires_in':TICKET_LIFETIME}

    @app.get('/api/business/admin/audit', dependencies=[Depends(authorized)])
    def admin_business_audit():
        with db.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM business_audit ORDER BY at DESC LIMIT 100')]
