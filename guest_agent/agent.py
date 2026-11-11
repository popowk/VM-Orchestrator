"""Windows Guest Agent: zero third-party Python packages, outbound HTTPS only.

Does not run arbitrary host-provided commands. Software updates require locally
approved install adapters, a host-approved immutable ZIP, and SHA-256 matching.
"""
from __future__ import annotations
from datetime import datetime, timezone
import argparse
import hashlib
import json
import ipaddress
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import re
import time
import urllib.error
import urllib.request
import uuid
import zipfile

MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_PRESERVE = {'data','config','profiles','credentials','logs','soul.db','fish-soul-config.json'}


def atomic_json(path: Path, data: dict):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def api(base, path, method='GET', data=None, headers=None, ssl_context=None, binary=False):
    body = json.dumps(data).encode('utf-8') if data is not None else None
    h = dict(headers or {})
    if body is not None: h['Content-Type']='application/json'
    req = urllib.request.Request(base.rstrip('/')+path, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=90, context=ssl_context) as r:
        if binary:
            content = r.read(MAX_PACKAGE_BYTES + 1)
            if len(content)>MAX_PACKAGE_BYTES: raise RuntimeError('Package exceeds guest download limit')
            return content
        return json.loads(r.read(1_000_000).decode('utf-8'))


def safe_extract_zip(zpath: Path, dest: Path):
    with zipfile.ZipFile(zpath) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos)>20000 or sum(i.file_size for i in infos)>MAX_EXPANDED_BYTES:
            raise RuntimeError('Archive size/file count limit exceeded')
        names=[]
        for i in infos:
            name=i.filename.replace('\\','/')
            p=Path(name)
            if name.startswith('/') or ':' in name or any(x in ('..','') for x in name.split('/')):
                raise RuntimeError('Unsafe archive member path')
            if (i.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
                raise RuntimeError('Archive contains symlink')
            names.append(p)
        # Standard SnowLuma release ZIPs may wrap files in one top-level folder.
        strip = len({p.parts[0] for p in names})==1 and all(len(p.parts)>1 for p in names)
        for i,p in zip(infos,names):
            rel = Path(*p.parts[1:]) if strip else p
            if not rel.parts: continue
            dst=dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.resolve().is_relative_to(dest.resolve()): raise RuntimeError('Unsafe resolved destination')
            with z.open(i) as inp, dst.open('wb') as out:
                shutil.copyfileobj(inp,out)


def portable_update(config: dict, zip_path: Path, task_id: str, version: str, base_dir: Path):
    """Atomic directory swap; preserve customer data from existing program tree.

    Stop the app before execution. Old installation is retained for rollback.
    """
    program = Path(config['program_dir']).resolve()
    if not program.is_dir() or program.is_symlink(): raise RuntimeError('Configured portable program directory missing')
    if program == Path(program.anchor) or program.name.lower() in {'windows','users','program files','program files (x86)','programdata','system32'}:
        raise RuntimeError('Refusing system/root program directory')
    if any(program == base_dir.resolve() or base_dir.resolve() in program.parents for _ in [0]):
        raise RuntimeError('Guest Agent directory cannot be updated')
    for name in config.get('stop_process_names', []):
        if not isinstance(name,str) or len(name)>100 or not name.isascii() or not name.replace('.','').replace('-','').isalnum():
            raise RuntimeError('Invalid process name')
        if os.name=='nt':
            p=subprocess.run(['tasklist','/FI',f'IMAGENAME eq {name}'],capture_output=True,text=True,timeout=15)
            if name.lower() in p.stdout.lower(): raise RuntimeError(f'Stop {name} before updating')
    parent=program.parent
    staging=parent / ('.vmo-stage-'+task_id)
    backup=parent / ('.vmo-backup-'+program.name+'-'+task_id)
    if staging.exists() or backup.exists(): raise RuntimeError('An update staging path already exists')
    # Copy complete existing state: this leaves login data, configs and memory untouched.
    shutil.copytree(program,staging,symlinks=False)
    extracted = base_dir / 'working' / task_id
    extracted.mkdir(parents=True,exist_ok=False)
    try:
        safe_extract_zip(zip_path,extracted)
        preserved = set(DEFAULT_PRESERVE) | {p.lower() for p in config.get('preserve_relative', [])}
        for file in extracted.rglob('*'):
            if not file.is_file(): continue
            rel=file.relative_to(extracted)
            if any(segment.lower() in preserved for segment in rel.parts):
                continue
            dst=staging / rel
            dst.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(file,dst)
        for path in config.get('required_files', []):
            if not (staging / path).is_file(): raise RuntimeError('Updated program missing expected file '+path)
        # Rename within a filesystem: no removal of the original until staged copy is complete.
        os.replace(program,backup)
        try:
            os.replace(staging,program)
        except BaseException:
            os.replace(backup,program)
            raise
        # Persist backup for a deliberate rollback; never auto-delete customer files.
        return {'backup':str(backup),'version':version,'note':'Portable files replaced; customer data preserved; restart and verify application manually'}
    finally:
        shutil.rmtree(extracted,ignore_errors=True)
        if staging.exists(): shutil.rmtree(staging,ignore_errors=True)


def cli_update(config: dict, product: str, version: str, root: Path):
    """Explicit local opt-in. Never run command or arguments supplied by API."""
    mode=config.get('mode')
    if os.name=='nt' and not config.get('allow_machine_scoped_cli',False):
        raise RuntimeError('CLI updater is disabled by default: Guest Agent runs as SYSTEM, which is not the DSH desktop user')
    if product=='fish-soul' and mode=='dsh_plugin':
        exe=Path(config['dsh_executable'])
        profile=config.get('profile','local-dsh')
        if not profile.replace('-','').replace('_','').isalnum(): raise RuntimeError('Unsafe DSH profile')
        command=[str(exe),'plugin','--profile',profile,'add','dsh-fish-soul@'+version]
    elif product=='dsh' and mode=='npm_global':
        exe=Path(config['npm_executable'])
        command=[str(exe),'install','--global','@deepseek-ai/dsh@'+version]
    else:
        raise RuntimeError('No approved update adapter configured for this installation')
    if not exe.is_file(): raise RuntimeError('Configured updater executable missing')
    backup_paths=config.get('backup_paths',[])
    if not isinstance(backup_paths,list) or not backup_paths:
        raise RuntimeError('CLI update requires explicit backup_paths; refusing to risk DSH data')
    backups=root/'backups'/('cli-'+product+'-'+uuid.uuid4().hex)
    backups.mkdir(parents=True,exist_ok=False)
    for ix,raw in enumerate(backup_paths):
        src=Path(raw).resolve()
        if not src.exists() or src.is_symlink() or not src.is_absolute() or src==Path(src.anchor):
            raise RuntimeError('Invalid CLI backup source; refusing update')
        dest=backups/str(ix)
        if src.is_dir(): shutil.copytree(src,dest,symlinks=False)
        else: shutil.copy2(src,dest)
    # Only explicitly configured local executable; no shell or arbitrary command strings.
    result=subprocess.run(command,capture_output=True,text=True,timeout=900,check=False,shell=False)
    if result.returncode: raise RuntimeError(f'Approved {product} updater failed with exit code {result.returncode}; backup: {backups}')
    return {'backup':str(backups),'note':'Pinned CLI package installed; check program health and use prior pinned release if rollback is required'}


def safe_update_failure(exc: Exception) -> str:
    """Never echo arbitrary vendor output, paths, access tokens or user settings."""
    msg=str(exc).split(';',1)[0].strip()
    allowed={
        'LOCAL_MANAGED_LIFECYCLE_NOT_CONFIGURED','INVALID_MANAGED_LISTEN_PORT',
        'INVALID_MANAGED_HEALTH_URL','LOCAL_RESTART_TASK_NOT_FOUND',
        'SCHEDULED_START_TASK_FAILED','APP_HEALTH_TIMEOUT_AFTER_RESTART',
        'UPDATE_PACKAGE_MISSING_REQUIRED_FILES','STAGED_INSTALLATION_INCOMPLETE',
        'INSTALLATION_CONTAINS_LINKS','UNSAFE_PROGRAM_DIRECTORY',
        'UPDATE_FAILED_AND_ROLLBACK_NEEDS_MANUAL_RECOVERY',
        'DSH_DESKTOP_STILL_RUNNING','DSH_DESKTOP_VERSION_MISMATCH',
        'DSH_INSTALLER_NONZERO_EXIT','INSTALLER_SIGNER_NOT_TRUSTED_OR_SIGNATURE_INVALID',
        'TARGET_WINDOWS_USER_SID_REQUIRED','REAL_DSH_PLUGIN_PACKAGE_NAME_REQUIRED',
        'DSH_PLUGIN_UPDATE_FAILED','DSH_CLI_NOT_FOUND',
        'LOCAL_BACKUP_PATHS_REQUIRED','CANNOT_GRANT_STAGED_INSTALLER_READ_ACCESS',
        'NO_INTERACTIVE_USER_LOGGED_ON','ACTIVE_USER_SID_DOES_NOT_MATCH_LOCAL_ADAPTER',
        'APP_STILL_LISTENING_AFTER_TASK_END','LOCAL_DESKTOP_INSTALLER_CONSENT_REQUIRED',
        'LOCAL_PLUGIN_UPDATE_CONSENT_REQUIRED','DSH_RESTART_TASK_FAILED',
        'USER_UPDATE_TIMEOUT_OUTCOME_REQUIRES_REVIEW',
        'ROLLBACK_FAILED_MANUAL_RECOVERY_REQUIRED',
        'ROLLBACK_HEALTH_CHECK_FAILED_ORIGINAL_RESTORED',
    }
    return msg if msg in allowed else type(exc).__name__


def perform_update(task: dict, config: dict, root: Path, ctx, on_phase=None):
    prod=task['product']
    cfg=config.get('installations',{}).get(prod,{})
    opts=task.get('options') or {}
    if not isinstance(opts,dict):raise RuntimeError('INVALID_UPDATE_OPTIONS')
    mode=cfg.get('mode','disabled')
    if task.get('kind')=='PLUGIN_COMMAND':
        if prod!='fish-soul' or not isinstance(opts.get('command'),str):
            raise RuntimeError('INVALID_PLUGIN_UPDATE_TASK')
        try:from .quick_plugin_update import execute_plugin_command
        except ImportError:from quick_plugin_update import execute_plugin_command
        result=execute_plugin_command(opts['command'],task['version'],root,on_phase=on_phase)
        # An exit-0 command is not a verified product version. Do not write
        # installed_versions based solely on the requested version.
        return result
    if opts.get('mode')=='snowluma_direct' and prod=='snowluma':
        mode='snowluma_direct'
    if mode=='disabled': raise RuntimeError(f'{prod}: installation adapter has not been configured on this guest')
    version=task['version']
    if not version or not all(ch.isalnum() or ch in '.-_+' for ch in version):
        raise RuntimeError('Unsafe version')
    headers={'X-Agent-ID':config['agent_id'],'X-Agent-Key':config['agent_key']}
    content=api(config['server_url'],f"/agent/artifacts/{task['package_id']}",headers=headers,ssl_context=ctx,binary=True)
    if on_phase:on_phase('downloaded')
    checksum=hashlib.sha256(content).hexdigest()
    if checksum.lower()!=task['sha256'].lower(): raise RuntimeError('Package SHA-256 mismatch')
    root.mkdir(parents=True,exist_ok=True)
    archive=root / ('download-'+task['id']+'.zip')
    archive.write_bytes(content)
    try:
        if mode=='snowluma_direct':
            try:from .quick_updates import direct_snowluma_update
            except ImportError:from quick_updates import direct_snowluma_update
            result=direct_snowluma_update(opts,archive,task['id'],version,root,extractor=safe_extract_zip,on_phase=on_phase)
        elif mode=='portable_managed':
            try:from .update_lifecycle import managed_portable_update
            except ImportError:from update_lifecycle import managed_portable_update
            result=managed_portable_update(cfg,archive,task['id'],version,root,extractor=safe_extract_zip,on_phase=on_phase)
        elif mode=='portable':
            result=portable_update(cfg,archive,task['id'],version,root)
        elif mode=='dsh_plugin_user' and prod=='fish-soul':
            try:from .dsh_updates import plugin_user_update
            except ImportError:from dsh_updates import plugin_user_update
            result=plugin_user_update(cfg,version,root)
        elif mode=='desktop_installer' and prod=='dsh':
            try:from .dsh_updates import desktop_installer_update
            except ImportError:from dsh_updates import desktop_installer_update
            result=desktop_installer_update(cfg,archive,task['id'],version,root,safe_extract_zip)
        elif mode in {'dsh_plugin','npm_global'}:
            # ZIP acts as the approved pinned-version record; the CLI's registry download
            # is separate and requires explicit local trusted mode.
            result=cli_update(cfg,prod,version,root)
        else: raise RuntimeError('Unknown local adapter')
    finally:
        archive.unlink(missing_ok=True)
    if on_phase:on_phase('verified')
    # An HTTP listener or command exit is not proof of the newly running version.
    # Only persist a confirmed version for the simplified SnowLuma workflow.
    if not (isinstance(result,dict) and result.get('runtime_version_api')=='not_verified'):
        config.setdefault('installed_versions',{})[prod]=version
    return result


def perform_rollback(task: dict, config: dict):
    prod=task['product']
    cfg=config.get('installations',{}).get(prod,{})
    if cfg.get('mode') not in ('portable','portable_managed'):
        raise RuntimeError('Rollback is only supported for portable ZIP installs')
    origin=task.get('reference_id','')
    if not isinstance(origin,str) or not origin or not all(c in '0123456789abcdef-' for c in origin):
        raise RuntimeError('Invalid rollback reference')
    if cfg.get('mode')=='portable_managed':
        try:from .update_lifecycle import managed_portable_rollback
        except ImportError:from update_lifecycle import managed_portable_rollback
        return managed_portable_rollback(cfg,origin,task['id'],Path(cfg['program_dir']).parent)
    program=Path(cfg['program_dir']).resolve()
    old=program.parent/('.vmo-backup-'+program.name+'-'+origin)
    current_backup=program.parent/('.vmo-pre-rollback-'+program.name+'-'+task['id'])
    if not program.is_dir() or not old.is_dir() or current_backup.exists():
        raise RuntimeError('Known rollback backup missing or application no longer installed')
    # Preserving current data is intentional: rollback does not erase current state.
    os.replace(program,current_backup)
    try:
        os.replace(old,program)
    except BaseException:
        os.replace(current_backup,program)
        raise
    return {'previous_install_backup':str(current_backup),
            'note':'Portable program rolled back. Review login/session data, compatibility and restart manually.'}


def oobe_evidence(config: dict) -> dict:
    """Read local SetupComplete marker; verify account and Windows setup state.

    A reported result is not a hypervisor-level proof. It is authenticated by the
    enrolled per-VM Guest Agent and tied to the enrollment's VM ID.
    """
    if os.name != 'nt':
        return {'verified': False, 'stage': 'non_windows'}
    marker = Path(os.environ.get('ProgramData', 'C:/ProgramData')) / 'VMO-OOBE' / 'oobe-marker.json'
    try:
        evidence = json.loads(marker.read_text(encoding='utf-8-sig'))
        if evidence.get('vm_id') != config.get('vm_id'):
            return {'verified': False, 'stage': 'identity_mismatch'}
        if evidence.get('step') != 'first_logon_complete':
            return {'verified': False, 'stage': 'setup_incomplete'}
        account = evidence.get('username','')
        if account != 'Administrator':
            return {'verified': False, 'stage': 'account_mismatch'}
        result = subprocess.run(['net','user',account],capture_output=True,timeout=15)
        if result.returncode:
            return {'verified': False, 'stage': 'account_missing'}
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State') as k:
            state,_ = winreg.QueryValueEx(k, 'ImageState')
        current = os.environ.get('COMPUTERNAME','')
        expected = evidence.get('expected_hostname','')
        verified = state == 'IMAGE_STATE_COMPLETE' and current.casefold() == expected.casefold()
        return {'verified': verified, 'vm_id': config.get('vm_id'), 'stage': 'complete' if verified else 'rename_or_setup_pending',
                'hostname': current, 'expected_hostname': expected, 'image_state': state}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, ImportError):
        return {'verified': False, 'stage': 'not_ready'}


def guest_ipv4_addresses(gateway_url: str = '') -> dict:
    """Guest-reported interface addresses, informational ONLY (never authentication).

    Prefer Windows' IP adapter list. Fallback to local resolver; optionally
    identify the source IP selected by the OS for the configured Gateway route.
    Do not need administrator credentials or extra pip modules.
    """
    addresses = set()
    if os.name == 'nt':
        try:
            proc = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                 'Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | '
                 'Where-Object { $_.AddressState -eq "Preferred" } | '
                 'ForEach-Object { $_.IPAddress }'],
                capture_output=True, timeout=8, check=False)
            if proc.returncode == 0:
                for address in proc.stdout.decode('ascii', errors='ignore').splitlines():
                    address = address.strip()
                    try:
                        ip = ipaddress.IPv4Address(address)
                        if not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
                            addresses.add(str(ip))
                    except ipaddress.AddressValueError:
                        pass
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not addresses:
        try:
            for data in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = ipaddress.IPv4Address(data[4][0])
                if not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
                    addresses.add(str(ip))
        except (OSError, ValueError):
            pass
    primary = ''
    try:
        from urllib.parse import urlsplit
        host = urlsplit(gateway_url).hostname
        if host and ipaddress.ip_address(host).version == 4:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((host, 8766))  # UDP connect selects route; no packet is sent.
                address = sock.getsockname()[0]
                if address and not ipaddress.IPv4Address(address).is_loopback:
                    primary = address
                    addresses.add(address)
    except (OSError, ValueError, TypeError):
        pass
    return {'ipv4_addresses': sorted(addresses)[:16], 'primary_ipv4': primary,
            'ip_source': 'guest_report'}


def detected_installations(config: dict) -> dict:
    """Read-only discovery. Folder names are NOT authoritative installed versions."""
    findings = {}
    base = Path('C:/dsh')
    if os.name == 'nt' and base.is_dir():
        try:
            candidates = list(base.glob('SnowLuma*'))[:24]
            for candidate in candidates:
                if not candidate.is_dir() or not (candidate/'launcher.bat').is_file():
                    continue
                match = re.search(r'(?i)^SnowLuma[-_]?v?(\d+(?:\.\d+){1,3})', candidate.name)
                findings['snowluma'] = {'installed': True, 'version_hint': match.group(1) if match else '',
                                         'source': 'directory_name', 'version_verified': False}
                break
        except OSError:
            pass
        if (base/'DeepSeek Harness'/'DeepSeek Harness.exe').is_file():
            findings['dsh']={'installed':True,'version_hint':'', 'source':'executable_found', 'version_verified':False}
    return findings


def snapshot_versions(config: dict):
    versions=dict(config.get('installed_versions',{}))
    for product,cfg in config.get('installations',{}).items():
        root=cfg.get('program_dir')
        file=cfg.get('version_file')
        if root and file:
            p=Path(root)/file
            try: versions[product]=p.read_text(encoding='utf-8').strip()[:80]
            except OSError: pass
    return {'versions':versions,'hostname':os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME','unknown'),
            'guest_agent':'1.3.4','oobe':oobe_evidence(config),
            'install_modes':{k:v.get('mode','disabled') for k,v in config.get('installations',{}).items()},
            'detected_software':detected_installations(config),
            **guest_ipv4_addresses(config.get('server_url',''))}


def bootstrap_from_iso(config_path: Path):
    """Optionally load unique first-boot token from per-clone provisioning ISO.

    No client secrets are stored in the golden template image.
    """
    if os.name != 'nt' or config_path.exists(): return
    for letter in 'DEFGHIJKLMNOPQRSTUVWXYZ':
        volume=Path(letter+':/vmo-agent.json')
        ca=Path(letter+':/vmo-ca.pem')
        if not volume.is_file() or not ca.is_file(): continue
        try:
            data=json.loads(volume.read_text(encoding='utf-8-sig'))
            if not data.get('server_url','').startswith('https://') or not data.get('enrollment_token'):
                continue
            config_path.parent.mkdir(parents=True,exist_ok=True)
            ca_target=config_path.parent / 'host-only-ca.pem'
            ca_target.write_bytes(ca.read_bytes())
            data['ca_bundle']=str(ca_target)
            data['installations']={'dsh':{'mode':'disabled'},'fish-soul':{'mode':'disabled'},'snowluma':{'mode':'disabled'}}
            atomic_json(config_path,data)
            return
        except (OSError,ValueError):
            continue


def safe_failure_code(exc: Exception) -> str:
    """Only a category is logged; never include token, URL or response body."""
    if isinstance(exc, urllib.error.HTTPError):
        return 'HTTP_' + str(exc.code)
    if isinstance(exc, urllib.error.URLError):
        return 'URLError_' + type(exc.reason).__name__
    return type(exc).__name__


def write_health(root: Path, ok: bool, problem: str | None):
    path = root / 'sync-status.json'
    old = {}
    try:
        old = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    now = datetime.now(timezone.utc).isoformat()
    status = {'last_attempt_utc': now, 'last_success_utc': now if ok else old.get('last_success_utc'),
              'sync_ok': ok, 'failure_code': problem}
    atomic_json(path, status)


def agent_loop(config_path: Path, once: bool = False):
    root=config_path.parent
    logs=root/'logs'
    logs.mkdir(parents=True,exist_ok=True)
    logger=logging.getLogger('vmo_guest')
    logger.setLevel(logging.INFO)
    log_path = (logs/'agent.log').resolve()
    for old_handler in list(logger.handlers):
        if (not isinstance(old_handler, RotatingFileHandler) or
                Path(old_handler.baseFilename) != log_path or not log_path.exists()):
            logger.removeHandler(old_handler)
            old_handler.close()
    if not logger.handlers:
        handler=RotatingFileHandler(log_path,maxBytes=512*1024,backupCount=3,encoding='utf-8')
        formatter=logging.Formatter('%(asctime)s +08:00 %(levelname)s %(message)s')
        formatter.converter=lambda seconds:time.gmtime(seconds+8*3600)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    last_problem=None
    successful=False
    logger.info('Guest Agent started; running as scheduled SYSTEM task')
    while True:
        try:
            bootstrap_from_iso(config_path)
            cfg=json.loads(config_path.read_text(encoding='utf-8-sig'))
            base=cfg['server_url'].rstrip('/')
            # HTTPS is mandatory for credentials over virtual networks.
            if not base.startswith('https://'):
                raise RuntimeError('Guest Agent requires an HTTPS gateway')
            ctx=ssl.create_default_context(cafile=cfg.get('ca_bundle') or None)
            if not cfg.get('agent_key'):
                token=cfg.get('enrollment_token','')
                if not token: raise RuntimeError('Guest enrollment token not configured')
                identity=api(base,'/agent/enroll','POST',{'vm_id':cfg['vm_id'],'one_time_token':token},ssl_context=ctx)
                cfg.update(identity)
                cfg.pop('enrollment_token',None)
                atomic_json(config_path,cfg)
            headers={'X-Agent-ID':cfg['agent_id'],'X-Agent-Key':cfg['agent_key']}
            api(base,'/agent/heartbeat','POST',{'report':snapshot_versions(cfg)},headers=headers,ssl_context=ctx)
            if not successful:
                logger.info('Guest enrollment/heartbeat established for this VM')
            successful=True
            last_problem=None
            task=api(base,'/agent/tasks/next',headers=headers,ssl_context=ctx).get('task')
            if task:
                try:
                    result=(perform_rollback(task,cfg) if task.get('kind')=='ROLLBACK'
                            else perform_update(task,cfg,root,ctx))
                    atomic_json(config_path,cfg)
                    ok=True; msg=json.dumps(result,ensure_ascii=False)[:800]
                except Exception as exc:
                    ok=False;msg=str(exc)[:800]
                api(base,f"/agent/tasks/{task['id']}/result",'POST',{'succeeded':ok,'message':msg},headers=headers,ssl_context=ctx)
            write_health(root, True, None)
        except Exception as exc:
            # Never log tokens, URLs containing credentials, headers, or arbitrary response bodies.
            # Log the exception type only; diagnostics can use TCP tests separately.
            problem=safe_failure_code(exc)
            try: write_health(root, False, problem)
            except OSError: pass
            if problem != last_problem:
                logger.warning('Guest sync pending/failed: %s',problem)
                last_problem=problem
            successful=False
        if once: return successful and last_problem is None
        time.sleep(30)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--once',action='store_true')
    a=parser.parse_args()
    ok=agent_loop(Path(a.config),a.once)
    if a.once and not ok: raise SystemExit(1)
