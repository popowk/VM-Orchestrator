"""Locally approved DSH Desktop installer and profile-scoped plugin adapters.

SYSTEM performs verified backups and checks, but never installs plugins into
SYSTEM's profile. User-token execution requires a pinned local SID.
"""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

try:
    from .update_lifecycle import run_task, validate_task
    from .user_update_exec import run_as_user, SID_RE
except ImportError:
    from update_lifecycle import run_task, validate_task
    from user_update_exec import run_as_user, SID_RE

PACKAGE_RE = re.compile(r'^(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]{0,127}$')
PROFILE_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$')
THUMB_RE = re.compile(r'^[A-Fa-f0-9]{40}$')


def backup_paths(config: dict, root: Path, label: str) -> str:
    paths=config.get('backup_paths')
    if not isinstance(paths,list) or not paths or len(paths)>20:
        raise RuntimeError('LOCAL_BACKUP_PATHS_REQUIRED')
    target=root/'backups'/(label+'-'+__import__('uuid').uuid4().hex)
    target.mkdir(parents=True,exist_ok=False)
    sources=[]
    for raw in paths:
        if not isinstance(raw,str) or not Path(raw).is_absolute():
            raise RuntimeError('INVALID_BACKUP_SOURCE')
        p=Path(raw).resolve()
        if not p.exists() or p.is_symlink() or p==Path(p.anchor) or len(p.parts)<3:
            raise RuntimeError('INVALID_BACKUP_SOURCE')
        sources.append(p)
    for i,p in enumerate(sources):
        if p.is_dir():shutil.copytree(p,target/str(i),symlinks=False)
        else:shutil.copy2(p,target/str(i))
    (target/'sources.json').write_text(json.dumps([str(p) for p in sources]),encoding='utf-8')
    return str(target)


def run_ps_json(script: str) -> dict:
    encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
    proc=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
                        capture_output=True,timeout=35,check=False)
    if proc.returncode:raise RuntimeError('WINDOWS_SIGNATURE_OR_VERSION_QUERY_FAILED')
    try:return json.loads(proc.stdout.decode('utf-8-sig').strip())
    except (ValueError,UnicodeError) as exc:raise RuntimeError('INVALID_WINDOWS_VERSION_QUERY') from exc


def quote_ps(v):
    if not isinstance(v,str) or '\n' in v or '\r' in v or '\0' in v:
        raise ValueError('INVALID_LOCAL_VALUE')
    return "'"+v.replace("'","''")+"'"


def signed_installer_ok(exe:Path, expected_thumbprint:str):
    if not THUMB_RE.fullmatch(expected_thumbprint or ''):
        raise RuntimeError('LOCAL_PINNED_PUBLISHER_THUMBPRINT_REQUIRED')
    p=quote_ps(str(exe))
    details=run_ps_json("$s=Get-AuthenticodeSignature -LiteralPath "+p+"; "
        "@{status=[string]$s.Status; thumbprint=if($s.SignerCertificate){$s.SignerCertificate.Thumbprint}else{''}} | ConvertTo-Json -Compress")
    if details.get('status')!='Valid' or details.get('thumbprint','').casefold()!=expected_thumbprint.casefold():
        raise RuntimeError('INSTALLER_SIGNER_NOT_TRUSTED_OR_SIGNATURE_INVALID')


def installed_exe_version(exe:Path)->str:
    if not exe.is_file():raise RuntimeError('DESKTOP_EXECUTABLE_MISSING_AFTER_INSTALL')
    details=run_ps_json("$f=Get-Item -LiteralPath "+quote_ps(str(exe))+"; "
        "@{version=[string]$f.VersionInfo.ProductVersion; file=[string]$f.VersionInfo.FileVersion} | ConvertTo-Json -Compress")
    return details.get('version') or details.get('file') or ''


def _stop_task_if_any(cfg):
    t=cfg.get('restart_task')
    if t:
        validate_task(t)
        if not run_task('/Query',t):raise RuntimeError('RESTART_TASK_NOT_FOUND')
        run_task('/End',t)
        time.sleep(3)


def _restart_task_if_any(cfg):
    if cfg.get('restart_task') and not run_task('/Run',cfg['restart_task']):
        raise RuntimeError('DSH_RESTART_TASK_FAILED')


def plugin_user_update(config:dict, version:str,root:Path):
    """Use the real package and profile pinned by a local administrator."""
    if os.name!='nt':raise RuntimeError('WINDOWS_ONLY')
    package=config.get('package_name','')
    profile=config.get('profile','')
    exe=Path(config.get('dsh_executable',''))
    user_sid=config.get('user_sid','')
    if not PACKAGE_RE.fullmatch(package):raise RuntimeError('REAL_DSH_PLUGIN_PACKAGE_NAME_REQUIRED')
    if not PROFILE_RE.fullmatch(profile):raise RuntimeError('DSH_PROFILE_NOT_CONFIGURED')
    if not SID_RE.fullmatch(user_sid):raise RuntimeError('USER_SID_NOT_CONFIGURED')
    if not exe.is_file():raise RuntimeError('DSH_CLI_NOT_FOUND')
    if not config.get('allow_user_profile_update',False):
        raise RuntimeError('LOCAL_PLUGIN_UPDATE_CONSENT_REQUIRED')
    backup=backup_paths(config,root,'dsh-plugin')
    stopped=False
    try:
        _stop_task_if_any(config)
        stopped=True
        args=['plugin','--profile',profile,'add',package+'@'+version]
        # The packaged Desktop CLI supports its special desktop profile.
        outcome=run_as_user(str(exe),args,user_sid,timeout=900)
        if outcome['exit_code']!=0:
            raise RuntimeError('DSH_PLUGIN_UPDATE_FAILED; protected backup retained')
    finally:
        if stopped:_restart_task_if_any(config)
    return {'backup':backup,'profile':profile,'package':package,'exit_code':0,
            'note':'User-scoped plugin command completed; reopen Desktop to load the bundle'}


def _readable_staging(staging:Path):
    # Staged installer has no secrets, but the user-token process must be able to
    # execute it. Grant Authenticated Users READ/EXECUTE only, never WRITE.
    cmd=['icacls.exe',str(staging),'/inheritance:r','/grant:r',
         '*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F',
         '*S-1-5-11:(OI)(CI)RX','/T','/C']
    proc=subprocess.run(cmd,capture_output=True,timeout=30,check=False)
    if proc.returncode:raise RuntimeError('CANNOT_GRANT_STAGED_INSTALLER_READ_ACCESS')


def desktop_installer_update(cfg:dict,archive:Path,task_id:str,version:str,root:Path,extractor):
    """Run a locally pinned signed EXE installer in the intended user session.

    Only explicit silent args + pinned certificate accepted. No generic remote EXE
    execution; only package uploaded through administrator-approved update catalog.
    Desktop rollback is MANUAL: different EXE vendors have different semantics.
    """
    if os.name!='nt':raise RuntimeError('WINDOWS_ONLY')
    if not cfg.get('allow_desktop_installer',False):
        raise RuntimeError('LOCAL_DESKTOP_INSTALLER_CONSENT_REQUIRED')
    user_sid=cfg.get('user_sid','')
    if not SID_RE.fullmatch(user_sid):raise RuntimeError('TARGET_WINDOWS_USER_SID_REQUIRED')
    installed=Path(cfg.get('installed_executable',''))
    if not installed.is_absolute() or installed.name.lower()!='deepseek harness.exe':
        raise RuntimeError('LOCAL_DSH_DESKTOP_EXECUTABLE_REQUIRED')
    flags=cfg.get('silent_args',[])
    if not isinstance(flags,list) or not flags or len(flags)>8 or any(
        not isinstance(a,str) or not re.fullmatch(r'[-/A-Za-z0-9_=.:]{1,80}',a) for a in flags):
        raise RuntimeError('EXPLICIT_LOCALLY_APPROVED_SILENT_ARGUMENTS_REQUIRED')
    working=root/'working'/task_id
    if working.exists():raise RuntimeError('PREVIOUS_DESKTOP_UPDATE_STAGING_EXISTS')
    working.mkdir(parents=True)
    try:
        extractor(archive,working)
        installers=[p for p in working.rglob('*.exe') if p.is_file()]
        if len(installers)!=1:
            raise RuntimeError('DESKTOP_ZIP_MUST_CONTAIN_EXACTLY_ONE_SETUP_EXE')
        installer=installers[0]
        signed_installer_ok(installer,cfg.get('signer_thumbprint',''))
        backup=backup_paths(cfg,root,'dsh-desktop')
        _readable_staging(working)
        _stop_task_if_any(cfg)
        # Do not install while a manually launched desktop process still exists.
        # The local adapter is opted in only for the logged-on user SID.
        check=run_ps_json("$p=@(Get-CimInstance Win32_Process -Filter "+quote_ps("Name='DeepSeek Harness.exe'")+" -ErrorAction SilentlyContinue); "
            "@{count=$p.Count} | ConvertTo-Json -Compress")
        if int(check.get('count',0))>0:
            raise RuntimeError('DSH_DESKTOP_STILL_RUNNING; close it from the target user first')
        result=run_as_user(str(installer),flags,user_sid,timeout=1200)
        if result['exit_code'] not in (0,3010):
            raise RuntimeError('DSH_INSTALLER_NONZERO_EXIT; backup retained')
        # Some signed EXE installers spawn a child process before their parent
        # exits. Keep staged bytes available while waiting for real version.
        deadline=time.monotonic()+90
        actual=''
        while time.monotonic()<deadline:
            try:actual=installed_exe_version(installed)
            except RuntimeError:actual=''
            if actual==version or actual.startswith(version+'.'):break
            time.sleep(3)
        if not actual or not (actual==version or actual.startswith(version+'.')):
            raise RuntimeError('DSH_DESKTOP_VERSION_MISMATCH; backup retained; manual recovery required')
        _restart_task_if_any(cfg)
        return {'backup':backup,'version':actual,'health':'installed_version_verified',
                'reboot_required':result['exit_code']==3010,
                'note':'Signed desktop installer completed under configured interactive Windows user; GUI health requires validation'}
    finally:
        shutil.rmtree(working,ignore_errors=True)
