"""Single-click SnowLuma portable update for a host-admin selected C:\\dsh path.

The Guest never kills an arbitrary port PID. Every terminated process must either
have an executable/command line in the approved SnowLuma directory, or be a
child of one. An unrelated port owner prevents the update before any swap.
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
    from .update_lifecycle import tcp_open, health_ready, _journal
    from .interactive_session import launch_interactive
except ImportError:
    from update_lifecycle import tcp_open, health_ready, _journal
    from interactive_session import launch_interactive

_VERSION=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$')


def checked_dir(raw: str) -> Path:
    from pathlib import PureWindowsPath
    p=PureWindowsPath(raw)
    if (p.drive.casefold()!='c:' or len(p.parts)<3 or p.parts[1].casefold()!='dsh' or
        not p.name.casefold().startswith('snowluma') or
        any(x in ('..','.') for x in p.parts) or not p.is_absolute()):
        raise RuntimeError('UNSAFE_SNOWLUMA_INSTALL_DIRECTORY')
    target=Path(raw)
    # Refuse junctions or symlinks in the path chain; file copying must not
    # inadvertently traverse outside the approved root.
    for ancestor in (target, *target.parents):
        if ancestor.exists() and (ancestor.is_symlink() or
            (hasattr(os.path,'isjunction') and os.path.isjunction(ancestor))):
            raise RuntimeError('INSTALL_DIRECTORY_CONTAINS_LINK')
    if not target.is_dir() or not (target/'launcher.bat').is_file():
        raise RuntimeError('SNOWLUMA_LAUNCHER_NOT_FOUND')
    return target.resolve()


def artifact_version(stage: Path) -> str:
    """Trust a release manifest only, never the operator's typed version alone."""
    candidates=[stage/'VERSION',stage/'version.txt',stage/'package.json',
                stage/'resources'/'app'/'package.json',stage/'packages'/'core'/'package.json']
    for p in candidates:
        if not p.is_file():continue
        try:
            if p.suffix=='.json':
                obj=json.loads(p.read_text(encoding='utf-8-sig'))
                name=str(obj.get('name','')).lower()
                if 'snowluma' not in name:continue
                value=obj.get('version','')
            else:value=p.read_text(encoding='utf-8-sig').strip()
            if isinstance(value,str) and _VERSION.fullmatch(value):return value
        except (OSError,ValueError,UnicodeError):continue
    raise RuntimeError('SNOWLUMA_RELEASE_VERSION_NOT_VERIFIABLE')


def _ps_quote(value: str) -> str:
    return "'"+value.replace("'","''")+"'"


def stop_owned_snowluma(directory:Path, port:int=5099):
    """Stop only processes provably rooted in this installation, not any :5099 PID."""
    if os.name!='nt':raise RuntimeError('WINDOWS_ONLY')
    script=r'''
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath(__ROOT__).TrimEnd([char]92)+[char]92
$all=@(Get-CimInstance Win32_Process)
$owned=New-Object 'System.Collections.Generic.HashSet[int]'
foreach($p in $all){
  $exe=[string]$p.ExecutablePath; $line=[string]$p.CommandLine
  if($exe.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) -or
     $line.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0){
    [void]$owned.Add([int]$p.ProcessId)
  }
}
# Descendant PIDs of known installed-app roots are still owned by the app.
for($i=0;$i -lt 12;$i++){
  $changed=$false
  foreach($p in $all){if($owned.Contains([int]$p.ParentProcessId) -and
    -not $owned.Contains([int]$p.ProcessId)){
      [void]$owned.Add([int]$p.ProcessId);$changed=$true
  }}
  if(-not $changed){break}
}
$owners=@(Get-NetTCPConnection -LocalPort __PORT__ -State Listen -ErrorAction SilentlyContinue |
   Select-Object -ExpandProperty OwningProcess -Unique)
foreach($pid_ in $owners){
  if(-not $owned.Contains([int]$pid_)){
    throw 'SNOWLUMA_PORT_OWNED_BY_UNRELATED_PROCESS'
  }
}
foreach($pid_ in ($owned | Sort-Object -Descending)){
  Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
}
'''
    script=script.replace('__ROOT__',_ps_quote(str(directory))).replace('__PORT__',str(port))
    encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
                          stdin=subprocess.DEVNULL,capture_output=True,timeout=35,check=False)
    if result.returncode:
        raise RuntimeError('SNOWLUMA_PROCESS_STOP_REJECTED_OR_FAILED')
    for _ in range(30):
        if not tcp_open(port):return
        time.sleep(1)
    raise RuntimeError('SNOWLUMA_PORT_STILL_OPEN_AFTER_STOP')


def start_snowluma(program:Path, port:int=5099):
    if tcp_open(port):raise RuntimeError('SNOWLUMA_PORT_ALREADY_OCCUPIED')
    started=launch_interactive('call launcher.bat','cmd',str(program))
    if not started.get('launched'):raise RuntimeError('SNOWLUMA_INTERACTIVE_LAUNCH_FAILED')
    healthy=0
    for _ in range(50):
        if tcp_open(port) and health_ready('http://127.0.0.1:5099/'):
            healthy+=1
            if healthy>=2:return started
        else:healthy=0
        time.sleep(2)
    raise RuntimeError('SNOWLUMA_HEALTH_CHECK_FAILED')


def direct_snowluma_update(options, archive, task_id, version, root, *,extractor,on_phase=None):
    if os.name!='nt':raise RuntimeError('WINDOWS_ONLY')
    def phase(n):
        if on_phase:on_phase(n)
    program=checked_dir(options.get('install_dir',''))
    if not _VERSION.fullmatch(version):raise RuntimeError('INVALID_VERSION')
    backup=program.parent/('.vmo-backup-'+program.name+'-'+task_id)
    staged=program.parent/('.vmo-stage-'+task_id)
    extracted=root/'working'/task_id
    failed=program.parent/('.vmo-failed-'+program.name+'-'+task_id)
    if any(p.exists() for p in (backup,staged,extracted,failed)):
        raise RuntimeError('UPDATE_PATH_ALREADY_PRESENT')
    extracted.mkdir(parents=True)
    try:
        extractor(archive,extracted)
        if not (extracted/'launcher.bat').is_file():raise RuntimeError('SNOWLUMA_PACKAGE_MISSING_LAUNCHER')
        try: actual=artifact_version(extracted)
        except RuntimeError as exc:
            if str(exc)!='SNOWLUMA_RELEASE_VERSION_NOT_VERIFIABLE':raise
            actual=None  # Vendor ZIP has no recognized manifest; require fresh runtime report later.
        if actual is not None and actual!=version:raise RuntimeError('SNOWLUMA_TARGET_VERSION_MISMATCH')
        for x in program.rglob('*'):
            if x.is_symlink() or (hasattr(os.path,'isjunction') and os.path.isjunction(x)):
                raise RuntimeError('INSTALLATION_CONTAINS_LINKS')
        shutil.copytree(program,staged,symlinks=False)
        try:from .agent import DEFAULT_PRESERVE
        except ImportError:from agent import DEFAULT_PRESERVE
        preserve=set(DEFAULT_PRESERVE)|{'data','config','profiles','credentials','logs','user_data','storage','session'}
        for f in extracted.rglob('*'):
            if not f.is_file():continue
            rel=f.relative_to(extracted)
            if any(part.lower() in preserve for part in rel.parts):continue
            target=staged/rel;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(f,target)
    finally:shutil.rmtree(extracted,ignore_errors=True)
    phase('staged')
    swapped=False
    try:
        stop_owned_snowluma(program)
        phase('stopped')
        os.replace(program,backup)
        try:os.replace(staged,program)
        except BaseException:os.replace(backup,program);raise
        swapped=True;phase('swapped')
        from datetime import datetime, timezone
        started_at=datetime.now(timezone.utc)
        launch=start_snowluma(program)
        # Verify both the installed release metadata and a fresh application
        # report after this process restart; stale Agent registration isn't proof.
        try:installed=artifact_version(program)
        except RuntimeError as exc:
            if str(exc)!='SNOWLUMA_RELEASE_VERSION_NOT_VERIFIABLE':raise
            installed=None
        if installed is not None and installed!=version:raise RuntimeError('SNOWLUMA_POST_RESTART_VERSION_MISMATCH')
        reported_version=False
        try:from .app_registry import read_registry
        except ImportError:from app_registry import read_registry
        for _ in range(8):
            entry=read_registry(root).get('snowluma',{})
            raw=entry.get('reported_at','')
            try:
                stamp=datetime.fromisoformat(raw)
                reported_version=(stamp.tzinfo is not None and stamp>=started_at and entry.get('version')==version)
            except (TypeError,ValueError):reported_version=False
            if reported_version:break
            time.sleep(2)
        phase('healthy')
        _journal(root,task_id,'healthy',version=installed,backup=str(backup),runtime_verified=reported_version)
        return {'version':installed if installed else '', 'target_version':version, 'health':'http_healthy',
                'version_evidence':'fresh_plugin_report' if reported_version else ('installed_release_manifest' if installed else 'unverified'),
                'runtime_version_api':'fresh_report' if reported_version else 'not_verified',
                'backup':str(backup),'pid':launch.get('pid'),
                'note':'SnowLuma restarted and HTTP listener is healthy; installed release version and fresh application report are evaluated separately'}
    except BaseException as exc:
        if swapped:
            try:
                stop_owned_snowluma(program)
                os.replace(program,failed);os.replace(backup,program)
                start_snowluma(program)
                phase('rolled_back_healthy')
                _journal(root,task_id,'rolled_back_healthy',failed_tree=str(failed))
            except BaseException as rec:
                phase('recovery_required');_journal(root,task_id,'recovery_required',reason=type(rec).__name__)
                raise RuntimeError('SNOWLUMA_ROLLBACK_REQUIRES_MANUAL_RECOVERY') from rec
        elif staged.exists():shutil.rmtree(staged,ignore_errors=True)
        raise
