"""VMware Workstation Host Agent. Single process, single operation worker.

The 8765 listener can bind all interfaces, but admin access MUST be firewall/TLS restricted.
"""
import hmac
import hashlib
import secrets
import time
import asyncio
import re
import sqlite3
import shutil
import threading
import os
import zipfile
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, Response as FastResponse, StreamingResponse
from .vnc_screen import enable_vnc, capture_loopback_vnc, vnc_config

from .config import Settings
from .jobs import JobRunner
from .storage import StoreConflict, TemplateStore
from .fleet import FleetStore, FleetError
from .remote_ops import RemoteStore, RemoteError, UPLOAD_CHUNK, DEFAULT_UPLOAD_DIR
from .agent_update import AgentUpdateManager, build_bootstrap
from .vmrun import Vmrun, VmrunError
from .diagnostics import diagnose
from .safety import assert_powered_off, assert_safe_vm_delete_dir, MAX_SNAPSHOTS, _is_reparse_point
from .power_state import reported_power_state, suspend_evidence
from .unattend import provisioning_status, read_secret
from .admin_auth import AdminAuth, AdminLoginLocked
from .cleanup import CleanupManager, CleanupError
from .host_control import HostControl, HostControlError
from .portable_catalog import PortableCatalog, PortableCatalogError, inspect_zip
from .host_metrics import read_host_metrics

NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$')


class RegisterTemplate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    vmx_path: str = Field(min_length=4)


class SealAttestation(BaseModel):
    operator: str = Field(min_length=2, max_length=100)
    sysprep_completed: bool
    credentials_reviewed: bool
    guest_is_shutdown: bool
    understood_not_cryptographic_proof: bool
    unattend_bootstrap_installed: bool = False
    unattend_bootstrap_version: int = Field(default=0, ge=0, le=2)


class CloneRequest(BaseModel):
    name: str = Field(min_length=1, max_length=48, pattern=NAME_RE.pattern)
    template_id: str
    customer_ref: str | None = Field(default=None, max_length=100)
    start_after_clone: bool = False
    enable_screen: bool = False
    unattended_oobe: bool = False


class ConfirmUnattendCleanup(BaseModel):
    confirm_oobe_checked: bool = False


class PowerStop(BaseModel):
    mode: Literal['soft', 'hard'] = 'soft'
    confirm_data_loss: bool = False


class AgentUpgradeRequest(BaseModel):
    confirm: bool = False


class UiLogin(BaseModel):
    username: str = Field(min_length=3, max_length=48)
    password: str


class UiChangePassword(BaseModel):
    current_password: str
    new_password: str


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1,max_length=80)
    scope: Literal['admin','business'] = 'admin'



class SnapshotCreate(BaseModel):
    name: str = Field(min_length=1, max_length=48, pattern=NAME_RE.pattern)


class ConfirmRestore(BaseModel):
    confirm_data_loss: bool = False
    shutdown_first: bool = False
    confirm_graceful_shutdown: bool = False


class SnapshotDeleteRequest(BaseModel):
    shutdown_first: bool = False  # Deprecated and explicitly rejected
    confirm_graceful_shutdown: bool = False


class ReinstallVmRequest(BaseModel):
    template_id: str
    unattended_oobe: bool = True
    enable_screen: bool = True
    confirm_name: str
    confirm_erase_customer_data: bool = False
    confirm_backup_disk_space: bool = False

class UpdateRequest(BaseModel):
    package_id: str
    install_dir: str | None = None
    confirm_backup_and_compatibility: bool = False

class TemplateRemovalRequest(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=100)
    confirm_permanent_delete: bool = False
    confirm_keep_history: bool = False
    confirm_delete_history: bool = False


class DeleteVmRequest(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=48)
    confirm_permanent_delete: bool = False


def create_app(settings: Settings | None = None, vmrun: Vmrun | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    templates_root = settings.templates_root.resolve()
    vms_root = settings.vms_root.resolve()
    if vms_root == templates_root or vms_root.is_relative_to(templates_root) or templates_root.is_relative_to(vms_root):
        raise RuntimeError('VMs root and templates root must not overlap')
    vms_root.mkdir(parents=True, exist_ok=True)
    store = TemplateStore(settings.db_path)
    fleet = FleetStore(settings.db_path)
    portable_catalog = PortableCatalog(settings.db_path)
    remote = RemoteStore(settings.db_path)
    agent_updates = AgentUpdateManager(settings.db_path, Path(__file__).resolve().parent.parent / "guest_agent")
    admin_auth = AdminAuth(settings.db_path, settings.admin_key, os.environ.get('VMO_BUSINESS_KEY',''))
    # Uvicorn must run with one worker, without --reload.
    store.recover_interrupted()
    vmrun = vmrun or Vmrun(settings.vmrun_path)
    secret_root = settings.db_path.parent / 'provision-secrets'
    runner = JobRunner(store, vmrun, secret_root, settings.admin_key)
    app = FastAPI(title='VMware Workstation Host Agent', version='1.4.54',
                  description='Local management of sealed templates and VMware Workstation VMs.')

    # UI session is an HttpOnly, SameSite=Strict cookie signed using a key derived
    # from the existing API secret. X-UI-CSRF is mandatory for state changes.
    ui_dir = Path(__file__).parent / 'ui'
    cookie_name = 'vmo_ui_session'
    max_session_seconds = 8 * 3600

    def ui_hmac(message: str, purpose: str) -> str:
        key = hashlib.sha256((purpose + ':' + settings.admin_key).encode('utf-8')).digest()
        return hmac.new(key, message.encode('ascii'), hashlib.sha256).hexdigest()

    # Sessions are persisted and revocable; no administrator password in cookies.
    def verify_session(request: Request) -> str | None:
        token = request.cookies.get(cookie_name, '')
        return token if admin_auth.session(token) else None

    def csrf_value(raw: str) -> str:
        return ui_hmac(hashlib.sha256(raw.encode()).hexdigest(), 'csrf')

    def authorized(request: Request, x_admin_key: str | None = Header(default=None),
                   x_ui_csrf: str | None = Header(default=None),
                   x_vmo_api_key: str | None = Header(default=None)):
        # Legacy X-Admin-Key and new X-VMO-API-Key both consult the revocation DB.
        if admin_auth.check_api(x_vmo_api_key or x_admin_key, 'admin'):
            return
        session_raw = verify_session(request)
        if not session_raw:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if not x_ui_csrf or not hmac.compare_digest(x_ui_csrf, csrf_value(session_raw)):
                raise HTTPException(status_code=403, detail='CSRF protection failed')

    def browser_origin(raw: str) -> str:
        """Only administrator-configured HTTPS origins enter browser CSP."""
        try:
            p=urlsplit(raw.strip())
            if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
                return ''
            # urlsplit() itself validates malformed IPv6 bracket syntax.
            return f'https://{p.netloc}'
        except ValueError:
            return ''

    @app.middleware('http')
    async def safety_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        path=request.url.path
        # Applications now use the exact same origin as the customer portal.
        gateway_origin='' 
        business_origin=browser_origin(os.environ.get('VMO_PUBLIC_BUSINESS_ORIGIN',''))
        if path=='/client' or path.startswith('/client/'):
            # The parent client portal embeds a trusted app gateway iframe and
            # POSTs a one-use handoff ticket directly to it (no JavaScript token URL).
            # Never allow wildcard iframe or form targets.
            additions=(' '+gateway_origin) if gateway_origin else ''
            ancestor=("'self' "+business_origin) if business_origin else "'self'"
            response.headers['Content-Security-Policy'] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data: blob:; font-src 'self'; "
                "frame-src 'self'"+additions+"; form-action 'self'"+additions+
                "; frame-ancestors "+ancestor+"; base-uri 'none'")
            if business_origin:
                # X-Frame-Options DENY would override the explicit allowlisted
                # business-site frame-ancestors in compatible browser clients.
                if 'X-Frame-Options' in response.headers:
                    del response.headers['X-Frame-Options']
            else:
                response.headers['X-Frame-Options']='SAMEORIGIN'
        elif path=='/apps/open' or path.startswith('/apps/'):
            # Preserve the app gateway's tenant-bound frame-ancestors policy.
            if 'X-Frame-Options' in response.headers:del response.headers['X-Frame-Options']
        else:
            response.headers['X-Frame-Options']='DENY'
            if path.startswith('/ui/') or path=='/':
                response.headers['Content-Security-Policy'] = (
                    "default-src 'none'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self' data: blob:; "
                    "font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.get('/', include_in_schema=False)
    def dashboard(request: Request):
        # Never send the privileged console markup until the session cookie
        # has been validated by the server. A login overlay in JS is too late.
        page = 'index.html' if verify_session(request) else 'login.html'
        return FileResponse(ui_dir / page, media_type='text/html; charset=utf-8',
                            headers={'Vary': 'Cookie', 'Cache-Control': 'no-store'})

    @app.get('/ui/app-route-recover.js', include_in_schema=False)
    def app_route_recover_script():
        return FileResponse(ui_dir / 'app-route-recover.js',media_type='text/javascript; charset=utf-8',
                            headers={'Cache-Control':'no-store'})

    @app.get('/ui/login.css', include_in_schema=False)
    def login_styles():
        return FileResponse(ui_dir / 'login.css', media_type='text/css; charset=utf-8')

    @app.get('/ui/login.js', include_in_schema=False)
    def login_script():
        return FileResponse(ui_dir / 'login.js', media_type='text/javascript; charset=utf-8')

    @app.get('/ui/ui.css', dependencies=[Depends(authorized)], include_in_schema=False)
    def ui_styles():
        return FileResponse(ui_dir / 'ui.css', media_type='text/css; charset=utf-8', headers={'Cache-Control':'no-store'})

    @app.get('/ui/admin.js', dependencies=[Depends(authorized)], include_in_schema=False)
    def admin_ui_script():
        # One canonical browser asset: no missing submodule routes or stale cached UI.
        return FileResponse(ui_dir / 'admin.js', media_type='text/javascript; charset=utf-8',
                            headers={'Cache-Control':'no-store'})

    login_attempts: dict[str,tuple[int,float]] = {}

    @app.get('/ui/setup-status', include_in_schema=False)
    def ui_setup_status():
        return {'ready':admin_auth.has_user(), 'bootstrap_command':'Manage.cmd account'}

    @app.post('/ui/login', include_in_schema=False)
    def ui_login(payload: UiLogin, request: Request, response: Response):
        # Synology/ESA can terminate HTTPS and forward internally over HTTP.
        # Compare against the explicit admin-approved public origin, never
        # untrusted X-Forwarded-Host / X-Forwarded-Proto headers.
        origin = request.headers.get('origin', '').rstrip('/')
        local_origin = str(request.base_url).rstrip('/')
        configured_origin = os.environ.get('VMO_PUBLIC_CONSOLE_ORIGIN', '').strip().rstrip('/')
        if configured_origin:
            expected = browser_origin(configured_origin)
            if expected != configured_origin:
                raise HTTPException(503, 'Invalid VMO_PUBLIC_CONSOLE_ORIGIN configuration')
        else:
            expected = ''
        if origin and origin not in (local_origin, expected):
            raise HTTPException(403, 'Cross-origin login prohibited')
        # Rate limit failed logins by remote IP; credentials are checked server side.
        ip = request.client.host if request.client else 'unknown'
        now = time.monotonic()
        failure = login_attempts.get(ip, (0,0.0))
        if failure[1] > now:
            raise HTTPException(429, 'Too many login attempts')
        try:
            token = admin_auth.login(payload.username, payload.password)
        except AdminLoginLocked as exc:
            # Account lock is durable, rather than an in-memory proxy-IP ban.
            raise HTTPException(429, '连续输错3次，账号已锁定15分钟',
                                headers={'Retry-After': str(exc.retry_after)})
        if not token:
            count = failure[0] + 1
            # Keep an additional modest guard for unknown usernames, which do
            # not have an account row to lock, without locking all reverse-
            # proxied users after three attempts from one shared source IP.
            login_attempts[ip] = (count, now + 60 if count >= 10 else 0.0)
            raise HTTPException(401, 'Incorrect username or password')
        login_attempts.pop(ip,None)
        response.set_cookie(cookie_name, token, httponly=True, samesite='strict',
                            secure=(request.url.scheme == 'https' or bool(expected and origin == expected)), max_age=max_session_seconds, path='/')
        return {'authenticated': True, 'csrf': csrf_value(token), 'username': payload.username}

    @app.get('/ui/session', include_in_schema=False)
    def ui_session(request: Request):
        raw = verify_session(request)
        if not raw:
            raise HTTPException(401, 'Session expired')
        return {'authenticated': True, 'csrf': csrf_value(raw), 'username': admin_auth.session(raw)}

    @app.post('/ui/logout', include_in_schema=False)
    def ui_logout(request: Request, response: Response, x_ui_csrf: str | None = Header(default=None)):
        raw = verify_session(request)
        if raw and (not x_ui_csrf or not hmac.compare_digest(x_ui_csrf, csrf_value(raw))):
            raise HTTPException(403, 'CSRF protection failed')
        if raw: admin_auth.logout(raw)
        response.delete_cookie(cookie_name, path='/')
        return {'authenticated': False}

    @app.post('/ui/change-password', dependencies=[Depends(authorized)])
    def ui_change_password(payload: UiChangePassword, request: Request, response: Response):
        raw=verify_session(request)
        if not raw: raise HTTPException(403,'Password change requires an administrator browser session')
        username=admin_auth.session(raw)
        try: admin_auth.change_password(username,payload.current_password,payload.new_password)
        except ValueError as e: raise HTTPException(422,str(e))
        response.delete_cookie(cookie_name,path='/')
        return {'changed':True,'relogin_required':True}

    @app.get('/api/admin/api-keys', dependencies=[Depends(authorized)])
    def api_key_list():
        return admin_auth.keys()

    @app.post('/api/admin/api-keys', dependencies=[Depends(authorized)], status_code=201)
    def api_key_create(payload: ApiKeyCreate):
        try:return admin_auth.create_key(payload.name,payload.scope)
        except ValueError as e:raise HTTPException(422,str(e))

    @app.delete('/api/admin/api-keys/{key_id}', dependencies=[Depends(authorized)])
    def api_key_revoke(key_id: str):
        if not admin_auth.revoke_key(key_id):raise HTTPException(404,'Key missing or already revoked')
        return {'revoked':True,'id':key_id}

    def vmx_under_root(raw_path: str) -> Path:
        if not templates_root.is_dir():
            raise HTTPException(503, 'Templates root does not exist')
        try:
            path = Path(raw_path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise HTTPException(404, 'VMX not found')
        if path.suffix.casefold() != '.vmx' or not path.is_file() or not path.is_relative_to(templates_root):
            raise HTTPException(400, 'VMX must be a file inside templates root')
        return path

    def managed_vm(vm_id: str) -> tuple[dict, Path]:
        vm = store.get_vm(vm_id)
        if not vm:
            raise HTTPException(404, 'VM not found')
        if vm['state'] == 'DELETED':
            raise HTTPException(410, 'VM has been deleted')
        expected = vms_root / vm['name'] / (vm['name'] + '.vmx')
        if Path(vm['vmx_path']) != expected or not expected.is_file() or not expected.resolve().is_relative_to(vms_root):
            raise HTTPException(409, 'Managed VMX missing or outside VMs root')
        return vm, expected

    def runtime_state(vmx: Path, running_paths: set[str] | None) -> str:
        if running_paths is None:
            return 'UNKNOWN'
        if str(vmx.resolve()).casefold() in running_paths:
            return 'POWERED_ON'
        return reported_power_state(vmx, False)

    def running_set() -> set[str] | None:
        try:
            return {str(p.resolve()).casefold() for p in vmrun.list_running()}
        except VmrunError:
            return None

    def vm_detail(vm: dict, running: set[str] | None) -> dict:
        result = dict(vm)
        if vm['state'] == 'READY' and Path(vm['vmx_path']).is_file():
            result['power_state'] = runtime_state(Path(vm['vmx_path']), running)
            evidence=suspend_evidence(Path(vm['vmx_path']))
            result['power_warning']=evidence.get('warning') or ''
            result['suspend_reinstall_blocked']=evidence['state'] not in ('clear',)
        else:
            result['power_state'] = 'UNKNOWN'
        return result

    def job_accepted(job: dict) -> dict:
        return {'job_id': job['id'], 'vm_id': job['vm_id'], 'status': job['status'],
                'status_url': f"/api/jobs/{job['id']}"}

    @app.get('/health')
    def health():
        return {'status': 'ok', 'phase': 10, 'version': system_updater.current, 'pid': os.getpid(), 'capability': 'per-clone-oobe-template-bootstrap'}

    # App-level updates are NOT the per-Guest "software updates" facility.
    # These routes remain on the privileged Host listener only.
    from .system_updates import UpdateService, UpdateError
    system_updater = UpdateService(Path(__file__).resolve().parent.parent)
    cleanup_manager = CleanupManager(Path(__file__).resolve().parent.parent,
                                     vms_root, settings.db_path, store, fleet, vmrun)
    host_control = HostControl(Path(__file__).resolve().parent.parent)

    class ApplySystemUpdate(BaseModel):
        version: str = Field(min_length=5, max_length=24)
        confirm: bool = False

    @app.get('/api/system/update', dependencies=[Depends(authorized)])
    def system_update_status(check: bool = False):
        try:
            return system_updater.check() if check else system_updater.status()
        except UpdateError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get('/api/system/update/log', dependencies=[Depends(authorized)])
    def system_update_log():
        return system_updater.diagnostics()

    @app.post('/api/system/update/apply', dependencies=[Depends(authorized)], status_code=202)
    def system_update_apply(payload: ApplySystemUpdate):
        if not payload.confirm:
            raise HTTPException(status_code=422, detail='必须确认已备份并允许服务短暂中断')
        if any(j.get('status') in ('QUEUED', 'RUNNING') for j in store.list_jobs(limit=100)):
            raise HTTPException(status_code=409, detail='仍有 VM 任务执行中，请稍后再更新')
        try:
            return system_updater.install(payload.version)
        except UpdateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


    @app.get('/api/system/host/status', dependencies=[Depends(authorized)])
    def system_host_status():
        return host_control.status()

    @app.get('/api/system/host/logs', dependencies=[Depends(authorized)])
    def system_host_logs():
        return host_control.logs()

    class RestartHostRequest(BaseModel):
        confirm_restart: bool = False

    @app.post('/api/system/host/restart', dependencies=[Depends(authorized)], status_code=202)
    def system_host_restart(payload: RestartHostRequest, request: Request):
        # This privileged operation must originate from a logged-in admin's
        # browser with CSRF, not from a business/API-key integration.
        if not verify_session(request):
            raise HTTPException(403, '重启管理程序需要管理员网页登录')
        if not payload.confirm_restart:
            raise HTTPException(422, '请明确确认管理端将短暂断线')
        if any(j.get('status') in ('QUEUED','RUNNING') for j in store.list_jobs(limit=100)):
            raise HTTPException(409, '仍有 VM 管理任务在执行，暂不重启')
        if system_updater._state_active():
            raise HTTPException(409, '系统更新正在进行，不能同时重启管理程序')
        try:
            return host_control.restart()
        except HostControlError as exc:
            raise HTTPException(409, detail=str(exc)) from exc

    class CleanupDeleteRequest(BaseModel):
        items: list[dict] = Field(min_length=1, max_length=50)
        confirm_phrase: str
        confirm_backups: bool = False

    @app.get('/api/system/cleanup/scan', dependencies=[Depends(authorized)])
    def system_cleanup_scan():
        try:
            return cleanup_manager.scan()
        except CleanupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post('/api/system/cleanup/delete', dependencies=[Depends(authorized)])
    def system_cleanup_delete(payload: CleanupDeleteRequest):
        if payload.confirm_phrase != '永久清理':
            raise HTTPException(status_code=422, detail='请输入永久清理以确认不可恢复的删除')
        try:
            return cleanup_manager.delete(payload.items,
                                          confirm_backups=payload.confirm_backups)
        except CleanupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get('/api/host/metrics', dependencies=[Depends(authorized)])
    def host_metrics():
        return read_host_metrics(Path(__file__).resolve().parent.parent, vms_root)

    @app.get('/api/host/capabilities', dependencies=[Depends(authorized)])
    def capabilities():
        result = {
            'vmrun_present': settings.vmrun_path.is_file(),
            'templates_root_present': templates_root.is_dir(),
            'vms_root_present': vms_root.is_dir(),
            'clone_driver': 'vmrun-full', 'clone_api_enabled': True,
            'guest_enrollment_enabled': True,
            'guest_live_wss_available': True,
            'guest_remote_command_requires_local_opt_in': True,
            'guest_gateway_requires_https_host_only': True,
            'guest_gateway_configured': bool(os.environ.get('VMO_GUEST_GATEWAY_URL','').startswith('https://')
                                             and os.environ.get('VMO_GUEST_CA_PEM','')
                                             and Path(os.environ.get('VMO_GUEST_CA_PEM','')).is_file()),
            'unattended_media_supported': True,
            'unattended_template_bootstrap_required': True,
            'oobe_completion_verification': 'authenticated_guest_agent',
            'max_snapshots_per_vm': MAX_SNAPSHOTS,
            'snapshot_changes_require_powered_off': True,
        }
        result['vmrun_responding'] = running_set() is not None if result['vmrun_present'] else False
        result['clone_verified_on_host'] = False  # only actual clone job can verify support
        return result

    @app.get('/api/templates', dependencies=[Depends(authorized)])
    def list_templates():
        return store.list()

    @app.post('/api/templates', dependencies=[Depends(authorized)], status_code=201)
    def register_template(payload: RegisterTemplate):
        vmx = vmx_under_root(payload.vmx_path)
        try:
            return store.add(payload.name.strip(), vmx)
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'VMX already registered')

    @app.get('/api/templates/discover', dependencies=[Depends(authorized)])
    def discover_templates():
        """Read-only scanning. Finds VMX files, never registers or seals automatically."""
        from .template_catalog import discover_vmxtemplates
        if not templates_root.is_dir():
            raise HTTPException(409, f'Template root does not exist: {templates_root}')
        return {'root': str(templates_root), 'candidates': discover_vmxtemplates(templates_root, store.list())}

    @app.post('/api/templates/{template_id}/archive', dependencies=[Depends(authorized)])
    def archive_template(template_id: str, payload: 'TemplateRemovalRequest'):
        template = store.get(template_id)
        if not template:
            raise HTTPException(404, 'Template not found')
        if payload.confirm_name != template['name']:
            raise HTTPException(422, 'Type exact template name to archive')
        if fleet.default_template() == template_id:
            raise HTTPException(409, 'Choose another default template before archiving this one')
        try:
            return store.archive_template(template_id)
        except StoreConflict as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/templates/{template_id}/restore', dependencies=[Depends(authorized)])
    def restore_archived_template(template_id: str):
        template = store.get(template_id)
        if not template: raise HTTPException(404, 'Template not found')
        if template['status'] in ('FILES_PURGING', 'FILES_DELETED'):
            raise HTTPException(409, 'Template files have been purged or purge is incomplete; history cannot be restored as a VM')
        try:
            vmx_under_root(template['vmx_path'])
            return store.restore_template(template_id)
        except StoreConflict as exc:
            raise HTTPException(409, str(exc))

    @app.delete('/api/templates/{template_id}', dependencies=[Depends(authorized)])
    def remove_template_files(template_id: str, payload: 'TemplateRemovalRequest'):
        """Remove disks of an archived template, never its provenance records.

        Existing full clones and historical rows are not deleted. For safety,
        only the sole VM in a dedicated immediate child folder can be purged.
        No root-level / junction / locked / running template is accepted.
        """
        template = store.get(template_id)
        if not template:
            raise HTTPException(404, 'Template not found')
        if not (payload.confirm_permanent_delete and payload.confirm_keep_history
                and payload.confirm_name == template['name']):
            raise HTTPException(422, 'Exact name, permanent deletion, and preservation of history must be confirmed')
        if template['status'] != 'ARCHIVED':
            raise HTTPException(409, 'Archive this golden template before purging its disk files')
        if fleet.default_template() == template_id:
            raise HTTPException(409, 'Choose another default template first')
        if any(j['status'] in ('QUEUED', 'RUNNING') for j in store.list_jobs()):
            raise HTTPException(409, 'Wait for all VMware jobs to finish before deleting a golden template')
        # Do NOT block merely because full clones or deleted VM history refer to
        # this template. Their historical template_id foreign keys remain intact.
        vmx = vmx_under_root(template['vmx_path'])
        folder = vmx.parent
        if (folder == templates_root or folder.parent != templates_root or
                folder.is_symlink() or _is_reparse_point(folder)):
            raise HTTPException(409, 'Disk purge requires its own direct child folder under the template root; root-level templates can only be archived')
        if any(folder.rglob('*.lck')) or any(folder.rglob('*.vmss')):
            raise HTTPException(409, 'Template has lock or suspend files; diagnose before deletion')
        if len(list(folder.rglob('*.vmx'))) != 1:
            raise HTTPException(409, 'Folder contains multiple VMX files; disk purge refused')
        for entry in folder.rglob('*'):
            if _is_reparse_point(entry):
                raise HTTPException(409, 'Template directory contains a junction or symlink; deletion refused')
        try:
            assert_powered_off(vmx, vmrun, strict_locks=True)
        except VmrunError as exc:
            raise HTTPException(409, str(exc))
        # Full clones are independent, but a legacy linked clone may point to
        # the original disk. Detect such external references before deletion.
        from .template_catalog import find_linked_disk_references
        linked = find_linked_disk_references(vms_root, folder)
        if linked:
            raise HTTPException(409, 'A managed VM disk descriptor refers to this template: ' + linked[0])
        quarantine = templates_root / ('.vmo-purge-' + str(uuid.uuid4()))
        try:
            store.begin_archived_template_purge(template_id)
        except StoreConflict as exc:
            raise HTTPException(409, str(exc))
        renamed = False
        delete_started = False
        try:
            folder.rename(quarantine)
            renamed = True
            delete_started = True
            shutil.rmtree(quarantine)
            return {'deleted': True, 'template_id': template_id,
                    'files_removed': True, 'history_preserved': True,
                    'template': store.complete_archived_template_purge(template_id)}
        except Exception as exc:
            # rmtree may have removed some files before failing. Never mark
            # the source folder ARCHIVED/intact after an incomplete cleanup.
            # In that case retain FILES_PURGING for safe operator recovery.
            if not delete_started:
                if renamed and quarantine.exists() and not folder.exists():
                    try: quarantine.rename(folder)
                    except Exception: pass
                try: store.revert_archived_template_purge(template_id)
                except Exception: pass
            raise HTTPException(409, 'Template purge did not finish; inspect archived template state and .vmo-purge-* directories before retrying') from exc

    @app.post('/api/templates/{template_id}/forget-history', dependencies=[Depends(authorized)])
    def forget_template_history(template_id: str, payload: 'TemplateRemovalRequest'):
        """Remove the source audit/lineage records only after filesystem cleanup.

        Never delete a root-level template's disk content implicitly. If it was
        archived rather than purged, its VMX must have already been removed
        manually, with full ownership/dependency review by the operator.
        """
        t=store.get(template_id)
        if not t: raise HTTPException(404,'Template not found')
        if payload.confirm_name!=t['name'] or not payload.confirm_delete_history:
            raise HTTPException(422,'Exact template name and irreversible loss of source history are required')
        if fleet.default_template()==template_id:
            raise HTTPException(409,'Change the default template first')
        if any(j['status'] in ('QUEUED','RUNNING') for j in store.list_jobs()):
            raise HTTPException(409,'Wait until all VMware jobs finish')
        if t['status']=='ARCHIVED' and Path(t['vmx_path']).exists():
            raise HTTPException(409,'The archived VMX file still exists. Root-level templates require manual file cleanup; no automatic deletion of shared folders.')
        if t['status'] not in ('ARCHIVED','FILES_DELETED'):
            raise HTTPException(409,'Archive and safely remove template files before forgetting its history')
        try:
            return store.forget_template_history(template_id)
        except StoreConflict as exc:
            raise HTTPException(409,str(exc))

    @app.get('/api/templates/{template_id}', dependencies=[Depends(authorized)])
    def get_template(template_id: str):
        template = store.get(template_id)
        if not template:
            raise HTTPException(404, 'Template not found')
        return template

    @app.post('/api/templates/{template_id}/seal-attestation', dependencies=[Depends(authorized)])
    def seal_attestation(template_id: str, payload: SealAttestation):
        template = store.get(template_id)
        if not template:
            raise HTTPException(404, 'Template not found')
        if not all([payload.sysprep_completed, payload.credentials_reviewed,
                    payload.guest_is_shutdown, payload.understood_not_cryptographic_proof]):
            raise HTTPException(422, 'All attestation statements must be true')
        vmx = vmx_under_root(template['vmx_path'])
        try:
            if vmrun.is_running(vmx):
                raise HTTPException(409, 'Template is still running')
        except VmrunError:
            raise HTTPException(503, 'Cannot verify template power state')
        if list(vmx.parent.glob('*.lck')) or list(vmx.parent.glob('*.vmss')):
            raise HTTPException(409, 'Template contains lock/suspend state')
        try:
            if payload.unattend_bootstrap_version == 2 and not payload.unattend_bootstrap_installed:
                raise HTTPException(422, 'V1.2 bootstrap version requires explicit installation attestation')
            version = (payload.unattend_bootstrap_version or 1) if payload.unattend_bootstrap_installed else 0
            return store.seal_attested(template_id, payload.operator, payload.unattend_bootstrap_installed, version)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/vms', dependencies=[Depends(authorized)], status_code=202)
    def clone_full(payload: CloneRequest):
        source = store.get(payload.template_id)
        if not source or source['status'] != 'SEALED_ATTESTED':
            raise HTTPException(409, 'Template must be SEALED_ATTESTED')
        if payload.unattended_oobe and source.get('oobe_bootstrap_version', 0) < 2:
            raise HTTPException(409, '此模板使用旧版或未配置 OOBE 引导。请使用 V1.2 Administrator 引导重新制作新模板。')
        source_vmx = vmx_under_root(source['vmx_path'])
        try:
            if vmrun.is_running(source_vmx):
                raise HTTPException(409, 'Cannot clone a powered-on template')
        except VmrunError:
            raise HTTPException(503, 'Cannot verify template state')
        if list(source_vmx.parent.glob('*.lck')) or list(source_vmx.parent.glob('*.vmss')):
            raise HTTPException(409, 'Template has active lock/suspend state')
        dst_folder = vms_root / payload.name
        if dst_folder.exists():
            raise HTTPException(409, 'Destination folder already exists')
        dst = dst_folder / f'{payload.name}.vmx'
        try:
            vm, job = store.queue_clone(payload.template_id, payload.name, dst, payload.customer_ref)
        except StoreConflict as exc:
            raise HTTPException(409, str(exc))
        runner.submit(job['id'], vm['id'], 'CLONE_FULL', src=source_vmx, name=payload.name,
                      start_after_clone=payload.start_after_clone, enable_screen=payload.enable_screen,
                      unattended_oobe=payload.unattended_oobe,
                      fleet=fleet, guest_gateway_url=os.environ.get('VMO_GUEST_GATEWAY_URL',''),
                      guest_ca_path=Path(os.environ['VMO_GUEST_CA_PEM']) if os.environ.get('VMO_GUEST_CA_PEM') else None,
                      sibling_paths=[Path(item['vmx_path']) for item in store.list_vms() if item['id'] != vm['id']])
        return job_accepted(job)

    @app.post('/api/vms/{vm_id}/repair-clone-identity', dependencies=[Depends(authorized)], status_code=202)
    def repair_clone_identity(vm_id: str):
        """Repair only duplicate-identity validation failures from a completed full clone."""
        vm, vmx = managed_vm(vm_id)
        template = store.get(vm['template_id'])
        if not template or template['status'] != 'SEALED_ATTESTED':
            raise HTTPException(409, 'Source template is not sealed')
        src = vmx_under_root(template['vmx_path'])
        try:
            if vmrun.is_running(vmx):
                raise HTTPException(409, 'VM must be powered off for identity repair')
        except VmrunError:
            raise HTTPException(503, 'Cannot verify VM power state')
        if list(vmx.parent.glob('*.lck')) or list(vmx.parent.glob('*.vmss')):
            raise HTTPException(409, 'VM has lock/suspend state')
        try:
            job = store.queue_identity_repair(vm_id)
        except StoreConflict as exc:
            raise HTTPException(409, str(exc))
        runner.submit(job['id'], vm_id, 'REPAIR_CLONE_IDENTITY', src=src)
        return job_accepted(job)

    @app.get('/api/vms', dependencies=[Depends(authorized)])
    def list_vms():
        running = running_set()
        return [vm_detail(vm, running) for vm in store.list_vms()]

    @app.get('/api/vms/{vm_id}', dependencies=[Depends(authorized)])
    def get_vm(vm_id: str):
        vm = store.get_vm(vm_id)
        if not vm:
            raise HTTPException(404, 'VM not found')
        if vm['state'] == 'DELETED':
            raise HTTPException(410, 'VM has been deleted')
        return vm_detail(vm, running_set())

    @app.delete('/api/vms/{vm_id}', dependencies=[Depends(authorized)], status_code=202)
    def delete_vm(vm_id: str, payload: DeleteVmRequest):
        """Permanently delete exactly one managed VM directory, never a template.

        Requires a typed VM name and proof of complete power-off. The background
        worker performs all safety checks again before deleting any files.
        """
        vm = store.get_vm(vm_id)
        if not vm or vm['state'] == 'DELETED':
            raise HTTPException(404, 'VM not found')
        if not payload.confirm_permanent_delete or payload.confirm_name != vm['name']:
            raise HTTPException(422, 'Type the exact VM name and confirm permanent deletion')
        if vm['state'] not in {'READY', 'ERROR', 'DELETE_ERROR'}:
            raise HTTPException(409, 'VM is not deletable in its current state')
        vmx = vms_root / vm['name'] / (vm['name'] + '.vmx')
        if Path(vm['vmx_path']) != vmx:
            raise HTTPException(409, 'VM path does not match managed VM directory')
        try:
            assert_safe_vm_delete_dir(vmx, vms_root, allow_missing_vmx=vm['state'] != 'READY')
            assert_powered_off(vmx, vmrun, strict_locks=True)
        except VmrunError as exc:
            raise HTTPException(409, str(exc))
        try:
            job = store.queue_vm_delete(vm_id)
        except StoreConflict as exc:
            raise HTTPException(409, str(exc))
        runner.submit(job['id'], vm_id, 'DELETE_VM', vms_root=vms_root, fleet=fleet)
        return job_accepted(job)

    @app.get('/api/fleet/default-template', dependencies=[Depends(authorized)])
    def get_default_template():
        return {'template_id': fleet.default_template()}

    @app.post('/api/fleet/default-template/{template_id}', dependencies=[Depends(authorized)])
    def set_default_template(template_id: str):
        try: return {'template_id':fleet.set_default_template(template_id)}
        except FleetError as exc: raise HTTPException(409,str(exc))

    @app.post('/api/vms/{vm_id}/reinstall', dependencies=[Depends(authorized)], status_code=202)
    def reinstall_vm(vm_id: str, payload: ReinstallVmRequest):
        paid_instance_guard(vm_id)
        vm, vmx = managed_vm(vm_id)
        if vm['state'] != 'READY' or payload.confirm_name != vm['name'] or not payload.confirm_erase_customer_data or not payload.confirm_backup_disk_space:
            raise HTTPException(422,'Reinstall requires READY VM, exact name, irreversible data-loss and backup-space confirmation')
        template = store.get(payload.template_id)
        if not template or template['status'] != 'SEALED_ATTESTED':
            raise HTTPException(409,'A sealed golden template is required')
        if payload.unattended_oobe and template.get('oobe_bootstrap_version', 0) < 2:
            raise HTTPException(409, '重装来源模板没有 V1.2 Administrator OOBE 引导；先制作新模板，或取消无人值守选项。')
        src = vmx_under_root(template['vmx_path'])
        try:
            assert_powered_off(vmx,vmrun,strict_locks=True)
            if vmrun.is_running(src) or list(src.parent.glob('*.lck')) or list(src.parent.glob('*.vmss')):
                raise VmrunError('Golden template is running or locked')
            if any(t['status'] in ('QUEUED','CLAIMED') for t in fleet.tasks(vm_id)):
                raise VmrunError('A guest software update is still pending')
            job = store.queue_reinstall(vm_id, payload.template_id)
        except (VmrunError,StoreConflict) as exc:
            raise HTTPException(409,str(exc))
        runner.submit(job['id'],vm_id,'REINSTALL_FROM_TEMPLATE',src=src,name=vm['name'],vms_root=vms_root,
                      reinstall_template_id=payload.template_id,fleet=fleet,
                      unattended_oobe=payload.unattended_oobe,enable_screen=payload.enable_screen,
                      guest_gateway_url=os.environ.get('VMO_GUEST_GATEWAY_URL',''),
                      guest_ca_path=Path(os.environ['VMO_GUEST_CA_PEM']) if os.environ.get('VMO_GUEST_CA_PEM') else None)
        return job_accepted(job)

    @app.get('/api/fleet/reinstalls', dependencies=[Depends(authorized)])
    def reinstall_history(vm_id: str | None = None):
        return fleet.reinstalls(vm_id)

    @app.get('/api/fleet/guests', dependencies=[Depends(authorized)])
    def registered_guest_agents():
        links=remote.link_states()
        return [{**g,'wss':links.get(g['vm_id'],{'state':'never_connected'})} for g in fleet.guests()]

    @app.get('/api/vms/{vm_id}/agent-upgrade', dependencies=[Depends(authorized)])
    def agent_upgrade_status(vm_id: str):
        managed_vm(vm_id)
        guest = next((g for g in fleet.guests() if g['vm_id']==vm_id and not g['revoked_at']),None)
        presence=remote.link_presence(vm_id)
        info=agent_updates.latest(vm_id)
        running=(guest or {}).get('reported',{}).get('guest_agent')
        return {'installed_version':running,'bundled_version':agent_updates.version,
                'auto_update_enabled':os.environ.get('VMO_GUEST_AGENT_AUTO_UPDATE','1').strip().lower() not in ('0','off','false','no'),
                'wss':presence.get('state','never_connected'),'latest':info,
                'verified':bool(info and running==info['version'] and info.get('status')!='FAILED')}

    @app.post('/api/vms/{vm_id}/agent-upgrade', dependencies=[Depends(authorized)],status_code=202)
    def agent_upgrade(vm_id: str, payload: AgentUpgradeRequest):
        managed_vm(vm_id)
        if not payload.confirm:raise HTTPException(422,'Explicit administrator confirmation required')
        guest=next((g for g in fleet.guests() if g['vm_id']==vm_id and not g['revoked_at']),None)
        if not guest:raise HTTPException(409,'Guest must be enrolled before remote upgrade')
        if remote.link_presence(vm_id).get('state')!='connected':
            raise HTTPException(409,'WSS not connected; remote update cannot be dispatched')
        if (guest.get('reported') or {}).get('guest_agent')==agent_updates.version:
            raise HTTPException(409,'Guest already reports the bundled version')
        if agent_updates.recent_pending(vm_id):
            raise HTTPException(409,'Previous Agent upgrade still needs verification; inspect the last task')
        try:
            manifest=agent_updates.prepare(vm_id)
            command=build_bootstrap(manifest['id'],manifest['sha256'],manifest['version'])
            operation=remote.queue(vm_id,'COMMAND',{
                'shell':'powershell','command':command,'privilege':'system',
                'confirmed':True,'run_mode':'wait','timeout_seconds':120},ttl=420)
            agent_updates.attach(manifest['id'],operation)
        except (OSError,ValueError,RemoteError) as exc:
            raise HTTPException(409,'Unable to prepare verified Agent upgrade') from exc
        return {'operation_id':operation,'status':'QUEUED','target_version':manifest['version'],
                'note':'Task starts a separate updater. Success requires a new WSS heartbeat reporting the target version.'}

    @app.get('/api/fleet/portable/profiles', dependencies=[Depends(authorized)])
    def portable_program_profiles():
        return portable_catalog.all()

    class AutoDeployConfig(BaseModel):
        enabled: bool
        confirm_all: bool = False

    @app.post('/api/fleet/portable/profiles/{profile_id}/auto-deploy',
              dependencies=[Depends(authorized)])
    def set_portable_auto_deploy(profile_id: str, payload: AutoDeployConfig):
        if payload.enabled and not payload.confirm_all:
            raise HTTPException(422,'启用自动推送需要明确确认全部客户机范围')
        try:
            profile=portable_catalog.set_auto_deploy(profile_id,payload.enabled)
        except PortableCatalogError as exc:
            raise HTTPException(409,str(exc)) from exc
        result=fleet.reconcile_auto_deploy() if payload.enabled else {'queued':[],'skipped':[]}
        return {'profile':profile,'queued':len(result['queued']),
                'skipped':len(result['skipped']),
                'note':('自动推送已开启：当前在线 Guest 将尝试一次；新上线客户机自动接收。'
                        if payload.enabled else '自动推送已关闭，尚未领取的自动任务已取消。')}

    @app.post('/api/fleet/portable/packages', dependencies=[Depends(authorized)], status_code=201)
    async def upload_portable_package(
            request: Request, name: str, launcher: str,
            version_override: str = '', version_file: str = ''):
        """One ZIP/root-directory, an approved launcher name and optional version hints.

        The Host cannot inspect a Windows EXE's version reliably on Linux;
        Guest detects installed EXE ProductVersion after the update.
        """
        root=settings.db_path.parent/'approved_packages'
        root.mkdir(parents=True, exist_ok=True)
        package_id=str(uuid.uuid4())
        path=root/(package_id+'.zip')
        size=0
        checksum=hashlib.sha256()
        try:
            with path.open('xb') as output:
                async for part in request.stream():
                    size+=len(part)
                    if size>512*1024*1024:
                        raise HTTPException(413, 'ZIP 超过 512 MB 限制')
                    output.write(part)
                    checksum.update(part)
            if size<50 or not zipfile.is_zipfile(path):
                raise HTTPException(422, '必须上传有效的 ZIP 压缩包')
            profile=inspect_zip(path, launcher, display_name=name,
                                version_override=version_override,
                                version_file=version_file, sha256=checksum.hexdigest())
            with zipfile.ZipFile(path) as z:
                if z.testzip():
                    raise HTTPException(422, 'ZIP 文件 CRC 校验失败')
            portable_catalog.upsert(profile)
            fleet.add_package('portable-'+profile['id'],profile['version'],
                              checksum.hexdigest(),size,
                              'approved-portable-update.zip',package_id)
            portable_catalog.record_package(package_id,profile)
            auto=fleet.reconcile_auto_deploy() if portable_catalog.get(profile['id'])['auto_deploy'] else {'queued':[], 'skipped':[]}
            return {'package':fleet.package(package_id),'profile':portable_catalog.get(profile['id']),
                    'auto_deploy':{'queued_count':len(auto['queued']),'skipped_count':len(auto['skipped'])},
                    'note':('版本已经通过 ZIP 内容识别'
                            if profile['version_source'].startswith('zip:') else
                            '版本来自手动填写' if profile['version_source']=='manual' else
                            'ZIP 无版本清单；以压缩包 SHA-256 指纹跟踪发布版本')}
        except PortableCatalogError as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(422,str(exc)) from exc
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @app.get('/api/fleet/packages', dependencies=[Depends(authorized)])
    def packages():
        # Never offer discontinued adapters for new installs. Historical records
        # remain persisted for auditing and older task result reconciliation.
        active={p['active_package_id'] for p in portable_catalog.all()
                if p.get('active_package_id')}
        result=[item for item in fleet.packages()
                if item['product'].startswith('portable-p-') and item['id'] in active]
        for item in result:
            item.update(portable_catalog.release_metadata(item['id']))
        return result

    @app.delete('/api/fleet/packages/{package_id}', dependencies=[Depends(authorized)])
    def delete_update_package(package_id: str):
        try:
            return fleet.delete_package(package_id)
        except FleetError as exc:
            message=str(exc)
            if message in ('Invalid update package ID','Update package does not exist'):
                raise HTTPException(404,message) from exc
            raise HTTPException(409,message) from exc

    # Remote operations stay in the loopback-only administrator app. Guest side
    # authenticates independently on 8766 and cannot access this API.
    @app.get('/api/vms/{vm_id}/remote/operations', dependencies=[Depends(authorized)])
    def admin_remote_operations(vm_id: str, page: int = Query(default=1, ge=1, le=100000),
                                page_size: int = Query(default=15, ge=1, le=100), paged: bool = False):
        managed_vm(vm_id)
        remote.recover_stale(vm_id)
        if not paged:
            return remote.list(vm_id)
        total=remote.count(vm_id)
        pages=max(1,(total+page_size-1)//page_size)
        page=min(page,pages)
        return {'items':remote.list(vm_id,limit=page_size,offset=(page-1)*page_size),
                'total':total,'page':page,'page_size':page_size,'pages':pages}

    @app.get('/api/vms/{vm_id}/remote/operations/{op_id}', dependencies=[Depends(authorized)])
    def admin_remote_operation(vm_id: str, op_id: str):
        managed_vm(vm_id)
        op = remote.get(op_id)
        if not op or op['vm_id']!=vm_id:raise HTTPException(404,'Operation not found')
        return op

    @app.get('/api/vms/{vm_id}/remote/presence', dependencies=[Depends(authorized)])
    def admin_remote_presence(vm_id: str):
        managed_vm(vm_id)
        return remote.link_presence(vm_id)

    @app.get('/api/admin/events', dependencies=[Depends(authorized)], include_in_schema=False)
    async def admin_events(request: Request):
        """Session-protected server-sent notifications; never contains privileged data.

        This is a push signal, not a second unauthenticated source of task results.
        Consumers fetch results via the existing authenticated operation/job APIs.
        """
        async def stream():
            previous=None
            heartbeat=0
            while not await request.is_disconnected():
                try:
                    changes=remote.change_fingerprint()
                    with fleet.db() as db:
                        tasks=tuple(tuple(r) for r in db.execute(
                            "SELECT id,status,COALESCE(claimed_at,''),COALESCE(completed_at,'') "
                            'FROM guest_tasks ORDER BY created_at DESC,id DESC LIMIT 30'))
                    with store._conn() as db:
                        jobs=tuple(tuple(r) for r in db.execute(
                            "SELECT id,status,COALESCE(completed_at,'') "
                            'FROM jobs ORDER BY created_at DESC,id DESC LIMIT 30'))
                    state=(changes,tasks,jobs)
                    if state!=previous:
                        previous=state
                        yield 'event: changed\ndata: {}\n\n'
                        heartbeat=0
                    else:
                        heartbeat+=1
                        if heartbeat>=20:
                            yield ': keepalive\n\n'
                            heartbeat=0
                except (OSError,sqlite3.Error):
                    yield 'event: warning\ndata: {}\n\n'
                    return
                await asyncio.sleep(1)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={
            'Cache-Control':'no-cache, no-transform','X-Accel-Buffering':'no'})

    @app.post('/api/vms/{vm_id}/remote/diagnostic', dependencies=[Depends(authorized)],status_code=202)
    def admin_remote_diagnostic(vm_id: str, payload: dict):
        managed_vm(vm_id)
        try: oid=remote.queue(vm_id,'DIAGNOSTIC',{'name':payload.get('name')})
        except RemoteError as e:raise HTTPException(422,str(e))
        return {'operation_id':oid,'status':'QUEUED'}

    @app.post('/api/vms/{vm_id}/remote/command', dependencies=[Depends(authorized)],status_code=202)
    def admin_remote_command(vm_id: str, payload: dict):
        vm,_=managed_vm(vm_id)
        if payload.get('confirm') is not True:
            raise HTTPException(422,'Explicit administrator confirmation required')
        try:
            oid=remote.queue(vm_id,'COMMAND',{'shell':payload.get('shell'),
                'command':payload.get('command'),'privilege':payload.get('privilege'),'confirmed':True,
                'run_mode':payload.get('run_mode','wait'), 'cwd':payload.get('cwd') or '', 'timeout_seconds':payload.get('timeout_seconds',60)},ttl=420)
        except RemoteError as e:raise HTTPException(422,str(e))
        return {'operation_id':oid,'status':'QUEUED',
                'note':'Managed Guest trusts the authenticated host administrator; audit and output are retained'}

    @app.post('/api/vms/{vm_id}/remote/upload/{filename}', dependencies=[Depends(authorized)],status_code=202)
    async def admin_remote_upload(vm_id: str,filename:str,request:Request):
        """Streaming body to disk; never buffer the entire upload in Python RAM."""
        from .remote_ops import validate_upload_directory
        managed_vm(vm_id)
        directory=request.headers.get('x-vmo-upload-directory',DEFAULT_UPLOAD_DIR)
        try: directory=validate_upload_directory(directory)
        except RemoteError as e:raise HTTPException(422,str(e))
        temp=remote.files/(str(uuid.uuid4())+'.partial')
        h=hashlib.sha256();size=0
        try:
            with temp.open('xb') as out:
                async for chunk in request.stream():
                    if not chunk:continue
                    for start in range(0,len(chunk),UPLOAD_CHUNK):
                        part=chunk[start:start+UPLOAD_CHUNK]
                        out.write(part);h.update(part);size+=len(part)
            try: oid=remote.create_upload_from_path(vm_id,filename,temp,size,h.hexdigest(),directory)
            except RemoteError as e:raise HTTPException(422,str(e))
        except OSError as e:
            raise HTTPException(507,'宿主机保存上传文件失败：请检查存储空间与目录权限') from e
        finally:temp.unlink(missing_ok=True)
        return {'operation_id':oid,'status':'QUEUED','bytes':size,'directory':directory,
                'note':'文件已存储到宿主机，等待 Agent 拉取并校验后保存到指定目录；不自动执行'}

    @app.post('/api/vms/{vm_id}/remote/files/list', dependencies=[Depends(authorized)],status_code=202)
    def admin_remote_file_list(vm_id: str, payload: dict):
        managed_vm(vm_id)
        try: oid=remote.queue(vm_id,'FILES_LIST',{'area':payload.get('area')},ttl=180)
        except RemoteError as e:raise HTTPException(422,str(e))
        return {'operation_id':oid,'status':'QUEUED'}

    @app.post('/api/vms/{vm_id}/remote/files/read', dependencies=[Depends(authorized)],status_code=202)
    def admin_remote_file_read(vm_id: str, payload: dict):
        managed_vm(vm_id)
        try: oid=remote.queue(vm_id,'FILE_READ',{'area':'logs','filename':payload.get('filename')},ttl=180)
        except RemoteError as e:raise HTTPException(422,str(e))
        return {'operation_id':oid,'status':'QUEUED'}

    @app.get('/api/vms/{vm_id}/remote/apps', dependencies=[Depends(authorized)])
    def admin_remote_apps(vm_id: str):
        managed_vm(vm_id)
        return remote.apps(vm_id)

    @app.post('/api/vms/{vm_id}/remote/apps/{app_id}/reveal', dependencies=[Depends(authorized)])
    def admin_app_code(vm_id: str, app_id: str, payload: dict):
        managed_vm(vm_id)
        if payload.get('confirm') is not True:raise HTTPException(422,'Explicit access-code reveal confirmation required')
        try: code=remote.app_secret(vm_id,app_id)
        except (RemoteError,RuntimeError) as e:raise HTTPException(409,str(e))
        if code is None:raise HTTPException(404,'No access code registered')
        reported=next((a.get('secret_reported_at') for a in remote.apps(vm_id) if a['app_id']==app_id),None)
        return FastResponse(content=json.dumps({'access_code':code,'reported_at':reported}),media_type='application/json',
                            headers={'Cache-Control':'no-store'})

    @app.get('/api/fleet/updates', dependencies=[Depends(authorized)])
    def update_tasks(vm_id: str | None = None, page: int = Query(default=1, ge=1, le=100000),
                     page_size: int = Query(default=15, ge=1, le=100), paged: bool = False):
        if not paged:
            return fleet.tasks(vm_id)
        total = fleet.count_tasks(vm_id)
        pages = max(1, (total+page_size-1)//page_size)
        page = min(page, pages)
        return {'items': fleet.tasks(vm_id,limit=page_size,offset=(page-1)*page_size),
                'total': total, 'page': page, 'page_size': page_size, 'pages': pages}

    @app.post('/api/vms/{vm_id}/software-update', dependencies=[Depends(authorized)],status_code=202)
    def software_update(vm_id: str, payload: UpdateRequest):
        if not payload.confirm_backup_and_compatibility:
            raise HTTPException(422,'Operator must confirm compatibility, local agent adapter, and backup plan')
        if payload.install_dir:
            raise HTTPException(422,'旧版指定安装目录方式已停用；请上传单顶层程序目录 ZIP')
        p=fleet.package(payload.package_id)
        if not p or not p['product'].startswith('portable-p-'):
            raise HTTPException(422,'旧版软件更新包已停用，请使用便携 ZIP 软件版本库')
        try:
            task_id=fleet.queue_update(vm_id,payload.package_id,options={})
        except FleetError as exc: raise HTTPException(409,str(exc))
        return {'task_id':task_id,'vm_id':vm_id,'status':'QUEUED',
                'note':'Guest 将按登记的程序目录执行备份替换，并保留更新前的运行状态。'}

    class PortableDeployRequest(BaseModel):
        package_id: str
        scope: Literal['one','all']
        vm_id: str | None = None
        confirm: bool = False
        confirm_all: bool = False

    @app.post('/api/fleet/portable/deploy', dependencies=[Depends(authorized)], status_code=202)
    def portable_deploy(payload: PortableDeployRequest):
        """One operator action, one or all READY VMs; missing program is first install.

        Each Guest independently re-verifies the package and actual directory.
        Stale/offline Agents are skipped, not silently queued for surprise
        execution after a future unrelated boot.
        """
        if not payload.confirm or (payload.scope=='all' and not payload.confirm_all):
            raise HTTPException(422,'请明确确认部署范围和运行状态保留规则')
        pkg=fleet.package(payload.package_id)
        if not pkg or not pkg['product'].startswith('portable-p-'):
            raise HTTPException(422,'只能部署已审核的便携软件 ZIP 包')
        if payload.scope=='one':
            if not payload.vm_id:
                raise HTTPException(422,'必须选择目标客户机')
            targets=[payload.vm_id]
        else:
            targets=[v['id'] for v in store.list_vms() if v['state']=='READY']
            if len(targets)>500:
                raise HTTPException(409,'目标超过500台，请缩小范围后分批部署')
        queued=[]
        skipped=[]
        for vm_id in targets:
            try:
                task=fleet.queue_update(vm_id,payload.package_id,options={})
                queued.append({'vm_id':vm_id,'task_id':task})
            except FleetError as exc:
                skipped.append({'vm_id':vm_id,'reason':str(exc)[:160]})
        return {'scope':payload.scope,'total':len(targets),
                'queued':queued,'skipped':skipped,
                'note':'已安装则更新并恢复之前运行状态；未安装则首次安装并保持关闭。离线/版本不足/忙碌客户机不下发。'}

    @app.get('/api/jobs', dependencies=[Depends(authorized)])
    def list_jobs(vm_id: str | None = Query(default=None), page: int = Query(default=1, ge=1, le=100000),
                  page_size: int = Query(default=20, ge=1, le=100), paged: bool = False):
        if not paged:
            return store.list_jobs(vm_id)
        total = store.count_jobs(vm_id)
        pages = max(1, (total+page_size-1)//page_size)
        page = min(page, pages)
        return {'items':store.list_jobs(vm_id,limit=page_size,offset=(page-1)*page_size),
                'total':total,'page':page,'page_size':page_size,'pages':pages}

    @app.get('/api/jobs/{job_id}', dependencies=[Depends(authorized)])
    def get_job(job_id: str):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, 'Job not found')
        return job

    def paid_instance_guard(vm_id: str):
        with sqlite3.connect(str(settings.db_path)) as d:
            try:
                row=d.execute('SELECT status,expires_at FROM business_services WHERE vm_id=?',(vm_id,)).fetchone()
            except sqlite3.OperationalError as exc:
                if 'no such table: business_services' in str(exc).lower():
                    return  # Unbound / standalone VM before BusinessDb initializes.
                raise HTTPException(503, 'Cannot verify paid-instance lifecycle; retry later') from exc
        if row and (row[0]!='ACTIVE' or (row[1] and __import__('datetime').datetime.fromisoformat(row[1].replace('Z','+00:00')).timestamp()<=time.time())):
            raise HTTPException(403,'Paid customer instance suspended or expired')

    def queue_vm_action(vm_id: str, action: str, *, snap: str | None = None, mode: str = 'soft'):
        if action in ('START','RESTORE_SNAPSHOT','REINSTALL'):paid_instance_guard(vm_id)
        vm, vmx = managed_vm(vm_id)
        if vm['state'] != 'READY':
            raise HTTPException(409, 'VM is not ready')
        try:
            job = store.queue_action(vm_id, action, detail=f'Snapshot={snap}' if snap else '')
        except StoreConflict as exc:
            raise HTTPException(409, str(exc))
        runner.submit(job['id'], vm_id, action, snap=snap, mode=mode)
        return job_accepted(job)

    @app.post('/api/vms/{vm_id}/start', dependencies=[Depends(authorized)], status_code=202)
    def start_vm(vm_id: str):
        return queue_vm_action(vm_id, 'START')

    @app.post('/api/vms/{vm_id}/stop', dependencies=[Depends(authorized)], status_code=202)
    def stop_vm(vm_id: str, payload: PowerStop | None = None):
        payload = payload or PowerStop()
        if payload.mode == 'hard' and not payload.confirm_data_loss:
            raise HTTPException(422, 'Hard stop requires confirm_data_loss=true')
        return queue_vm_action(vm_id, 'STOP', mode=payload.mode)

    @app.get('/api/vms/{vm_id}/diagnostics', dependencies=[Depends(authorized)])
    def vm_diagnostics(vm_id: str):
        vm, vmx = managed_vm(vm_id)
        result = diagnose(vmx, vmrun)
        result.update({'vm_id': vm_id, 'vm_state': vm['state'],
                       'active_jobs': [j['action'] for j in store.list_jobs(vm_id)
                                       if j['status'] in {'QUEUED','RUNNING'}]})
        return result

    @app.get('/api/vms/{vm_id}/unattend', dependencies=[Depends(authorized)])
    def unattended_status(vm_id: str):
        vm, vmx = managed_vm(vm_id)
        info = provisioning_status(secret_root, vm_id, vmx)
        if info['prepared']:
            try:
                secret = read_secret(secret_root, vm_id, settings.admin_key)
                info.update({'username': secret['username'], 'hostname': secret['hostname']})
            except VmrunError as exc:
                raise HTTPException(409, str(exc))
        return info

    @app.post('/api/vms/{vm_id}/unattend/reveal-password', dependencies=[Depends(authorized)])
    def reveal_unattended_password(vm_id: str):
        vm, vmx = managed_vm(vm_id)
        try:
            info = read_secret(secret_root, vm_id, settings.admin_key)
        except VmrunError as exc:
            raise HTTPException(409, str(exc))
        if not info:
            raise HTTPException(404, 'This VM has no unattended provisioning secret')
        return info  # local authenticated administrator only; no caching or logging

    @app.post('/api/vms/{vm_id}/unattend/cleanup', dependencies=[Depends(authorized)], status_code=202)
    def cleanup_unattend(vm_id: str, payload: ConfirmUnattendCleanup):
        if not payload.confirm_oobe_checked:
            raise HTTPException(422, 'Explicit confirmation of guest initialization required')
        vm, vmx = managed_vm(vm_id)
        if vm['state'] != 'READY':
            raise HTTPException(409, 'VM not ready')
        diag = diagnose(vmx, vmrun)
        if not diag['offline_vmx_edit_possible']:
            raise HTTPException(409, diag['reason'])
        try:
            job = store.queue_action(vm_id, 'UNATTEND_CLEANUP')
        except StoreConflict as exc:
            raise HTTPException(409, str(exc))
        runner.submit(job['id'], vm_id, 'UNATTEND_CLEANUP')
        return job_accepted(job)

    @app.post('/api/vms/{vm_id}/enable-screen', dependencies=[Depends(authorized)])
    def enable_screen(vm_id: str):
        """Configure VMware built-in VNC, loopback only, on an OFF managed VM."""
        vm, vmx = managed_vm(vm_id)
        if vm['state'] != 'READY':
            raise HTTPException(409, 'VM must be ready')
        if any(j['status'] in {'QUEUED','RUNNING'} for j in store.list_jobs(vm_id)):
            raise HTTPException(409, 'VM operation in progress')
        diag = diagnose(vmx, vmrun)
        if not diag['offline_vmx_edit_possible']:
            raise HTTPException(409, diag['reason'])
        used = set()
        for other in store.list_vms():
            op = Path(other['vmx_path'])
            if op.is_file() and op != vmx:
                config = vnc_config(op)
                if config.get('port', '').isdigit():
                    used.add(int(config['port']))
        try:
            result = enable_vnc(vmx, used)
            if diag['lock_markers']:
                result['warning'] = diag['warning']
            return result
        except VmrunError as exc:
            raise HTTPException(409, str(exc))

    @app.get('/api/vms/{vm_id}/screen.png', dependencies=[Depends(authorized)])
    def screenshot(vm_id: str):
        """No screen is persisted to disk; screenshot is authenticated and uncached."""
        vm, vmx = managed_vm(vm_id)
        if vm['state'] != 'READY':
            raise HTTPException(409, 'VM not ready')
        try:
            if not vmrun.is_running(vmx):
                raise HTTPException(409, 'VM is powered off')
            png = capture_loopback_vnc(vmx)
            return FastResponse(content=png, media_type='image/png', headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate',
                'Content-Disposition': 'inline; filename="screen.png"',
            })
        except VmrunError as exc:
            raise HTTPException(503, str(exc))

    @app.get('/api/vms/{vm_id}/screen-preview.jpg', dependencies=[Depends(authorized)])
    def screen_preview(vm_id: str):
        """Opt-in admin-only 960x540 JPEG snapshot, local VMX VNC only.

        Neither the snapshot nor any long-running stream is persisted. The UI
        controls polling and never starts it unless the operator opts in.
        """
        vm, vmx = managed_vm(vm_id)
        if vm['state'] != 'READY':
            raise HTTPException(409, 'VM not ready')
        try:
            if not vmrun.is_running(vmx):
                raise HTTPException(409, 'VM is powered off')
            jpeg = capture_loopback_vnc(vmx, preview=True)
            return FastResponse(content=jpeg, media_type='image/jpeg', headers={
                'Cache-Control': 'no-store, private, max-age=0',
                'X-Content-Type-Options': 'nosniff',
            })
        except VmrunError as exc:
            raise HTTPException(503, str(exc))

    @app.get('/api/vms/{vm_id}/firstboot-state', dependencies=[Depends(authorized)])
    def firstboot_state(vm_id: str):
        vm, vmx = managed_vm(vm_id)
        powered = runtime_state(vmx, running_set())
        if powered != 'POWERED_ON':
            return {'power_state': powered, 'tools_state': 'not_checked',
                    'guest_ip': None, 'oobe_complete': None,
                    'unattend': provisioning_status(secret_root, vm_id, vmx)}
        try:
            tools_state = vmrun.check_tools_state(vmx)
        except VmrunError:
            tools_state = 'unknown'
        try:
            guest_ip = vmrun.guest_ip(vmx)
        except VmrunError:
            guest_ip = None
        guest_record = next((g for g in fleet.guests() if g['vm_id'] == vm_id and not g.get('revoked_at')), None)
        report = (guest_record or {}).get('reported') or {}
        oobe = report.get('oobe') or {}
        confirmed = (oobe.get('verified') is True and oobe.get('vm_id') == vm_id)
        return {'power_state': powered, 'tools_state': tools_state,
                'guest_ip': guest_ip, 'oobe_complete': True if confirmed else None,
                'oobe_evidence': {'agent_heartbeat': (guest_record or {}).get('last_seen'),
                                  'report': oobe},
                'note': 'OOBE complete only when an authenticated Guest Agent confirms SetupComplete, account and Windows ImageState; VMware Tools/IP alone do not prove it.',
                'unattend': provisioning_status(secret_root, vm_id, vmx)}

    @app.get('/api/vms/{vm_id}/snapshots', dependencies=[Depends(authorized)])
    def list_snapshots(vm_id: str):
        vm, vmx = managed_vm(vm_id)
        if vm['state'] != 'READY':
            raise HTTPException(409, 'VM not ready')
        if any(j['status'] in {'RUNNING', 'QUEUED'} for j in store.list_jobs(vm_id)):
            raise HTTPException(409, 'VM operation in progress')
        try:
            snapshots = vmrun.snapshots(vmx)
            return {'vm_id': vm_id, 'snapshots': snapshots, 'count': len(snapshots),
                    'limit': MAX_SNAPSHOTS, 'can_create': len(snapshots) < MAX_SNAPSHOTS}
        except VmrunError as exc:
            raise HTTPException(503, str(exc))

    @app.post('/api/vms/{vm_id}/snapshots', dependencies=[Depends(authorized)], status_code=202)
    def create_snapshot(vm_id: str, payload: SnapshotCreate):
        vm, vmx = managed_vm(vm_id)
        if vm['state'] != 'READY':
            raise HTTPException(409, 'VM is not ready')
        ensure_snapshot_off(vmx)
        try:
            if len(vmrun.snapshots(vmx)) >= MAX_SNAPSHOTS:
                raise HTTPException(409, f'Maximum {MAX_SNAPSHOTS} snapshots per VM; delete an old snapshot first')
        except VmrunError as exc:
            raise HTTPException(503, str(exc))
        return queue_vm_action(vm_id, 'SNAPSHOT_CREATE', snap=payload.name)

    @app.post('/api/vms/{vm_id}/snapshots/{snapshot_name}/restore', dependencies=[Depends(authorized)], status_code=202)
    def restore_snapshot(vm_id: str, snapshot_name: str, payload: ConfirmRestore):
        paid_instance_guard(vm_id)
        if not NAME_RE.fullmatch(snapshot_name):
            raise HTTPException(422, 'Invalid snapshot name')
        if not payload.confirm_data_loss:
            raise HTTPException(422, 'Snapshot restore requires confirm_data_loss=true')
        if payload.shutdown_first:
            raise HTTPException(422, 'Automatic shutdown before snapshot restore is no longer supported; shut down the VM yourself')
        vm, vmx = managed_vm(vm_id)
        if vm['state'] != 'READY':
            raise HTTPException(409, 'VM is not ready')
        ensure_snapshot_off(vmx)
        return queue_vm_action(vm_id, 'SNAPSHOT_RESTORE', snap=snapshot_name)

    @app.delete('/api/vms/{vm_id}/snapshots/{snapshot_name}', dependencies=[Depends(authorized)], status_code=202)
    def delete_snapshot(vm_id: str, snapshot_name: str, payload: SnapshotDeleteRequest | None = None):
        if not NAME_RE.fullmatch(snapshot_name):
            raise HTTPException(422, 'Invalid snapshot name')
        payload = payload or SnapshotDeleteRequest()
        if payload.shutdown_first:
            raise HTTPException(422, 'Automatic shutdown before snapshot deletion is no longer supported; shut down the VM yourself')
        vm, vmx = managed_vm(vm_id)
        if vm['state'] != 'READY':
            raise HTTPException(409, 'VM is not ready')
        ensure_snapshot_off(vmx)
        return queue_vm_action(vm_id, 'SNAPSHOT_DELETE', snap=snapshot_name)

    # Customer self-service uses a separate, tenant-bound session; never admin authorization.
    from .client_portal import install_client_portal
    install_client_portal(app, settings=settings, store=store, fleet=fleet,
                          vmrun=vmrun, runner=runner, templates_root=templates_root,
                          vms_root=vms_root, authorized=authorized,
                          vm_detail=vm_detail, running_set=running_set,
                          managed_vm=managed_vm, clone_full=clone_full,
                          queue_vm_action=queue_vm_action, screenshot=screenshot,
                          create_snapshot=create_snapshot,
                          restore_snapshot=restore_snapshot,
                          delete_snapshot=delete_snapshot,
                          reinstall_vm=reinstall_vm, software_update=software_update)
    from .same_origin_apps import install_same_origin_app_proxy
    install_same_origin_app_proxy(app)
    return app


app = create_app()
