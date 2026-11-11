"""Locally opted-in Windows application lifecycle for approved Guest updates.

No arbitrary host command is accepted. A pre-created interactive Scheduled Task is
owned by the local Guest administrator and must already be usable by the target user.
Only one updater runs at once; changing program files without stopping the service
is refused. A failed health check rolls portable files back without deleting any tree.
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
import uuid
import urllib.error
import urllib.request
import json

_TASK = re.compile(r'^\\VMO-[A-Za-z0-9_-]{3,64}$')


def validate_task(task: str) -> str:
    if not isinstance(task,str) or not _TASK.fullmatch(task):
        raise ValueError('Restart task must be a locally registered \\VMO-* task')
    return task


def tcp_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.8)
        return s.connect_ex(('127.0.0.1', port)) == 0


def run_task(action: str, name: str) -> bool:
    """Run or end a locally preconfigured task. Never synthesize a shell command."""
    validate_task(name)
    if action not in ('/Run','/End','/Query'):
        raise ValueError('Unexpected task action')
    proc=subprocess.run(['schtasks.exe',action,'/TN',name],capture_output=True,timeout=25,check=False)
    return proc.returncode == 0


def health_ready(url: str) -> bool:
    """401/403 still prove that the application HTTP listener is active."""
    from urllib.parse import urlsplit
    p=urlsplit(url)
    if p.scheme!='http' or p.hostname!='127.0.0.1' or p.username or p.password or p.query or p.fragment:
        raise ValueError('Only 127.0.0.1 HTTP health endpoints are supported')
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'VMO-Guest-Health'}),timeout=3) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code in (401,403,404)
    except (urllib.error.URLError,TimeoutError,OSError):
        return False


def lifecycle_config(cfg: dict) -> tuple[int,str,str]:
    life=cfg.get('lifecycle',{})
    if not isinstance(life,dict) or life.get('enabled') is not True:
        raise RuntimeError('LOCAL_MANAGED_LIFECYCLE_NOT_CONFIGURED')
    port=life.get('port')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise RuntimeError('INVALID_MANAGED_LISTEN_PORT')
    task=validate_task(life.get('restart_task',''))
    url=life.get('health_url','')
    if not isinstance(url,str) or not url.startswith(f'http://127.0.0.1:{port}/'):
        raise RuntimeError('INVALID_MANAGED_HEALTH_URL')
    return port,task,url


def stop_managed(port:int, task:str, delay:int=25):
    # If the task did not own the process, it may remain online. Refuse swapping
    # files rather than indiscriminately terminating the port's process/children.
    if not run_task('/Query',task):
        raise RuntimeError('LOCAL_RESTART_TASK_NOT_FOUND')
    run_task('/End',task)   # /End returns nonzero when the task is not running
    deadline=time.monotonic()+delay
    while time.monotonic()<deadline:
        if not tcp_open(port):return
        time.sleep(1)
    raise RuntimeError('APP_STILL_LISTENING_AFTER_TASK_END; close manually or migrate startup to managed task')


def start_managed(port:int, task:str,url:str, delay:int=90):
    if not run_task('/Run',task):
        raise RuntimeError('SCHEDULED_START_TASK_FAILED; ensure target Windows user is logged on')
    deadline=time.monotonic()+delay
    consecutive=0
    while time.monotonic()<deadline:
        if tcp_open(port) and health_ready(url):
            consecutive+=1
            if consecutive>=2:return
        else:consecutive=0
        time.sleep(2)
    raise RuntimeError('APP_HEALTH_TIMEOUT_AFTER_RESTART')


def _move(src:Path,dest:Path):
    os.replace(src,dest)


def _journal(root:Path, task_id:str, phase:str, **extra):
    journal=root/'update-journals'
    journal.mkdir(parents=True,exist_ok=True)
    path=journal/(task_id+'.json')
    temp=journal/(task_id+'.tmp')
    temp.write_text(json.dumps({'task_id':task_id,'phase':phase,**extra},ensure_ascii=False),encoding='utf-8')
    os.replace(temp,path)


def managed_portable_update(config, zip_path, task_id, version, base_dir, *, extractor, on_phase=None):
    """Stop via task -> stage -> swap -> start -> healthy. On failed start restore.

    The failed new tree is retained for forensic review; the original backup is
    preserved until a verified restart. No blind kill and no blind retry.
    """
    if os.name!='nt':
        raise RuntimeError('WINDOWS_ONLY')
    def phase(name):
        if on_phase:on_phase(name)
    port,task,url=lifecycle_config(config)
    program=Path(config['program_dir']).resolve()
    if (not program.is_dir() or program.is_symlink() or program==Path(program.anchor)
            or program.name.lower() in ('windows','users','program files','programdata','system32')
            or program == base_dir.resolve() or base_dir.resolve() in program.parents):
        raise RuntimeError('UNSAFE_PROGRAM_DIRECTORY')
    required=config.get('required_files',[])
    if not isinstance(required,list) or 'launcher.bat' not in required:
        raise RuntimeError('MISSING_LOCAL_LAUNCHER_VALIDATION')
    backup=program.parent/('.vmo-backup-'+program.name+'-'+task_id)
    stage=program.parent/('.vmo-stage-'+task_id)
    failed=program.parent/('.vmo-failed-'+program.name+'-'+task_id)
    extracted=base_dir/'working'/task_id
    if any(p.exists() for p in (backup,stage,failed,extracted)):
        raise RuntimeError('PREVIOUS_UPDATE_PATH_EXISTS; manual review required')
    if not run_task('/Query',task):
        raise RuntimeError('LOCAL_RESTART_TASK_NOT_FOUND')
    # Fail before stopping the running app if the package is invalid.
    extracted.mkdir(parents=True)
    try:
        extractor(zip_path,extracted)
        if not all((extracted/x).is_file() for x in required):
            raise RuntimeError('UPDATE_PACKAGE_MISSING_REQUIRED_FILES')
        if any(x.is_symlink() or (hasattr(os.path,'isjunction') and os.path.isjunction(x)) for x in program.rglob('*')):
            raise RuntimeError('INSTALLATION_CONTAINS_LINKS; manual review required')
        shutil.copytree(program,stage,symlinks=False)
        try:from .agent import DEFAULT_PRESERVE
        except ImportError:from agent import DEFAULT_PRESERVE
        preserved=set(DEFAULT_PRESERVE)|{x.lower() for x in config.get('preserve_relative',[])}
        for f in extracted.rglob('*'):
            if not f.is_file():continue
            rel=f.relative_to(extracted)
            if any(part.lower() in preserved for part in rel.parts):continue
            dst=stage/rel
            dst.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(f,dst)
        if not all((stage/x).is_file() for x in required):
            raise RuntimeError('STAGED_INSTALLATION_INCOMPLETE')
    finally:
        shutil.rmtree(extracted,ignore_errors=True)
    swapped=False
    try:
        _journal(base_dir,task_id,'staged',program=str(program))
        phase('staged')
        stop_managed(port,task)
        _journal(base_dir,task_id,'stopped',program=str(program))
        phase('stopped')
        _move(program,backup)
        try:_move(stage,program)
        except BaseException:
            _move(backup,program)
            raise
        swapped=True
        _journal(base_dir,task_id,'swapped',backup=str(backup))
        phase('swapped')
        start_managed(port,task,url)
        _journal(base_dir,task_id,'healthy',backup=str(backup))
        phase('healthy')
        return {'backup':str(backup),'version':version,'health':'verified',
                'note':'Scheduled Task restarted and HTTP endpoint passed two health checks'}
    except BaseException as err:
        if swapped:
            try:
                stop_managed(port,task)
                _move(program,failed)
                _move(backup,program)
                _journal(base_dir,task_id,'rollback_swapped',failed_tree=str(failed))
                start_managed(port,task,url)
                _journal(base_dir,task_id,'rolled_back_healthy',failed_tree=str(failed))
                phase('rolled_back_healthy')
            except BaseException as rollback_exc:
                _journal(base_dir,task_id,'recovery_required',error=type(rollback_exc).__name__)
                phase('recovery_required')
                raise RuntimeError('UPDATE_FAILED_AND_ROLLBACK_NEEDS_MANUAL_RECOVERY') from rollback_exc
        else:
            if stage.exists():shutil.rmtree(stage)
            _journal(base_dir,task_id,'not_applied',reason=type(err).__name__)
        raise


def managed_portable_rollback(cfg, origin, task_id, base_dir):
    port,task,url=lifecycle_config(cfg)
    program=Path(cfg['program_dir']).resolve()
    old=program.parent/('.vmo-backup-'+program.name+'-'+origin)
    current=program.parent/('.vmo-pre-rollback-'+program.name+'-'+task_id)
    if not old.is_dir() or not program.is_dir() or current.exists():
        raise RuntimeError('KNOWN_ROLLBACK_BACKUP_MISSING')
    stop_managed(port,task)
    _move(program,current)
    try:_move(old,program)
    except BaseException:
        _move(current,program)
        raise
    try:start_managed(port,task,url)
    except BaseException:
        try:
            stop_managed(port,task)
            _move(program,old)
            _move(current,program)
            start_managed(port,task,url)
        except BaseException as exc:
            raise RuntimeError('ROLLBACK_FAILED_MANUAL_RECOVERY_REQUIRED') from exc
        raise RuntimeError('ROLLBACK_HEALTH_CHECK_FAILED_ORIGINAL_RESTORED')
    return {'previous_install_backup':str(current),'health':'verified','note':'Previous release restored and healthy'}
