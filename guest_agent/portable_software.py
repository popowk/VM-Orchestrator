"""Versioned C:\dsh portable application updates for VMO Guest Agent V2.

One archive = one application directory. Preserve the original run state:
stopped apps remain stopped. The backup stays outside the installed folder,
and a failed swap or failed restart restores the previous bytes.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
import zipfile

VERSION = re.compile(r'^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$')
FOLDER = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]{1,75}$')
ALLOWED_START = {'.exe', '.bat', '.cmd'}
PRESERVE = ('data', 'config', 'profiles', 'credentials', 'logs', 'sessions',
            'soul.db', 'fish-soul-config.json', '.env')


def safe_relative(value: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise RuntimeError('PORTABLE_RELATIVE_FILE_INVALID')
    segments = value.replace('\\', '/').split('/')
    if len(segments) > 5 or any(not s or s in ('.', '..') or
        re.search(r'[<>:"/\\|?*\x00-\x1f]',s) or s.endswith((' ','.')) for s in segments):
        raise RuntimeError('PORTABLE_RELATIVE_FILE_INVALID')
    return Path(*segments)


def program_path(folder: str, base: Path = Path('C:/dsh')) -> Path:
    if (not isinstance(folder, str) or not FOLDER.fullmatch(folder)
            or folder.strip()!=folder or folder.endswith(('.', ' '))
            or folder.casefold().startswith('.vmo-')
            or folder.casefold() in ('windows','users','system32','guestagent','programdata')):
        raise RuntimeError('PORTABLE_PROGRAM_FOLDER_INVALID')
    directory = base / folder
    if base.is_symlink() or directory.is_symlink():
        raise RuntimeError('PORTABLE_PROGRAM_DIRECTORY_LINK')
    return directory


def _no_links(root: Path) -> bool:
    for base, dirs, files in os.walk(root, followlinks=False):
        for item in dirs + files:
            candidate = Path(base) / item
            if candidate.is_symlink():
                return False
            if os.name == 'nt':
                try:
                    if candidate.stat().st_file_attributes & 0x400:
                        return False
                except (AttributeError, OSError):
                    return False
    return True


def exe_version(filename: Path) -> str:
    """Read Product/FileVersion from a PE EXE without optional pywin32 deps."""
    if os.name != 'nt' or not filename.is_file():
        return ''
    try:
        version = ctypes.WinDLL('version', use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
        version.GetFileVersionInfoSizeW.restype = ctypes.c_uint
        unused = ctypes.c_uint(0)
        length = version.GetFileVersionInfoSizeW(str(filename), ctypes.byref(unused))
        if not length or length > 2 * 1024 * 1024:
            return ''
        raw = ctypes.create_string_buffer(length)
        version.GetFileVersionInfoW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p]
        version.GetFileVersionInfoW.restype=ctypes.c_int
        if not version.GetFileVersionInfoW(str(filename),0,length,raw):
            return ''
        ptr=ctypes.c_void_p()
        count=ctypes.c_uint(0)
        version.VerQueryValueW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,
                                        ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(ctypes.c_uint)]
        version.VerQueryValueW.restype=ctypes.c_int
        if not version.VerQueryValueW(raw, '\\',ctypes.byref(ptr),ctypes.byref(count)):
            return ''
        if count.value < 13 * 4:
            return ''
        class FixedInfo(ctypes.Structure):
            _fields_=[(name, ctypes.c_uint32) for name in (
                'signature','struct_version','file_ms','file_ls','product_ms','product_ls',
                'flags_mask','flags','os','file_type','file_subtype','file_date_ms','file_date_ls')]
        info=ctypes.cast(ptr,ctypes.POINTER(FixedInfo)).contents
        if info.signature != 0xFEEF04BD:
            return ''
        high=info.product_ms if info.product_ms else info.file_ms
        low=info.product_ls if info.product_ls else info.file_ls
        fields=(high>>16,high & 0xffff,low>>16,low & 0xffff)
        return '.'.join(map(str, fields)) if any(fields) else ''
    except (OSError, ValueError, AttributeError):
        return ''


def installed_version(directory: Path, launcher: str = '', version_file: str = '') -> dict:
    """Best-effort evidence: EXE metadata > explicit VERSION > manifest > name."""
    if version_file:
        requested = safe_relative(version_file)
        candidates=[requested]
    else:
        candidates=[Path('VERSION'),Path('version.txt'),Path('version'),
                    Path('package.json'),Path('app.json'),Path('manifest.json')]
    chosen = safe_relative(launcher) if launcher else None
    if chosen and chosen.suffix.lower()=='.exe':
        current=exe_version(directory / chosen)
        if current and VERSION.fullmatch(current):
            return {'version': current[:40], 'source': 'exe_version_info', 'verified': True}
    for file in candidates:
        target=directory / file
        try:
            if not target.is_file() or target.stat().st_size > 32768:
                continue
            data=target.read_text(encoding='utf-8-sig').strip()
            if file.suffix.lower()=='.json':
                payload=json.loads(data)
                value=payload.get('version') or payload.get('appVersion') if isinstance(payload,dict) else ''
            else:
                value=data.splitlines()[0].strip() if data else ''
            if isinstance(value,str) and VERSION.fullmatch(value):
                return {'version':value,'source':'file:' + file.as_posix(),'verified':True}
        except (OSError, UnicodeError, ValueError, TypeError, IndexError):
            continue
    for target in sorted(directory.glob('*.exe'))[:5]:
        current=exe_version(target)
        if current and VERSION.fullmatch(current):
            return {'version': current[:40], 'source': 'exe_version_info', 'verified': True}
    match=re.search(r'(?i)(?:^|[-_ ])v?(\d+\.\d+(?:\.\d+){0,2})',directory.name)
    return {'version':match.group(1) if match else '',
            'source':'folder_hint' if match else 'unavailable','verified':False}


def scan_c_dsh(max_entries: int = 32, base: Path = Path('C:/dsh')) -> dict:
    """Bounded, read-only inventory returned in the authenticated Guest heartbeat."""
    findings={}
    if not base.is_dir() or base.is_symlink():
        return findings
    try:
        for folder in sorted(base.iterdir(),key=lambda x:x.name.casefold()):
            if len(findings)>=max_entries:break
            if (not folder.is_dir() or folder.is_symlink() or
                folder.name.startswith('.') or folder.name.startswith('_') or
                not FOLDER.fullmatch(folder.name)):
                continue
            files=[x for x in sorted(folder.iterdir(),key=lambda x:x.name.casefold())
                   if x.is_file() and not x.is_symlink() and
                   x.suffix.lower() in ALLOWED_START][:20]
            preferred=next((x for x in files if x.name.lower()=='launcher.bat'),None)
            preferred=preferred or next((x for x in files if x.name.lower() in ('start.cmd','start.bat')),None)
            preferred=preferred or next((x for x in files if x.suffix.lower()=='.exe'),None)
            if not preferred:
                continue
            hint=installed_version(folder,preferred.name)
            findings[folder.name[:75]] = {
                'version_hint':hint['version'][:40],
                'version_verified':hint['verified'],
                'source':hint['source'],
                'launcher':preferred.name[:100],
            }
    except OSError:
        pass
    return findings


def _processes(helper: Path, directory: Path, launcher: Path, mode: str) -> dict:
    if os.name != 'nt':
        raise RuntimeError('PORTABLE_WINDOWS_GUEST_ONLY')
    if not helper.is_file():
        raise RuntimeError('PORTABLE_PROCESS_HELPER_NOT_INSTALLED')
    cmd=['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy',
         'RemoteSigned','-File',str(helper),'-Mode',mode,
         '-ProgramRoot',str(directory),'-Launcher',str(launcher)]
    result=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL,timeout=50,check=False,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        raise RuntimeError('PORTABLE_PROCESS_INSPECTION_OR_STOP_FAILED')
    try:
        response=json.loads(result.stdout.decode('utf-8-sig'))
        if not isinstance(response,dict) or not isinstance(response.get('running'),bool):
            raise ValueError()
        return response
    except (ValueError,UnicodeError) as exc:
        raise RuntimeError('PORTABLE_PROCESS_RESULT_INVALID') from exc


def _launch(program: Path, launcher: Path) -> dict:
    try:
        from .interactive_session import _start_process, _get_active_session
    except ImportError:
        from interactive_session import _start_process, _get_active_session
    session=_get_active_session()
    target=program/launcher
    if launcher.suffix.lower()=='.exe':
        argv=['exe',str(target)]
    else:
        # cmd.exe /C (not /K) avoids leaving a permanent terminal window.
        argv=['cmd.exe','/D','/C',f'call "{target}"']
    return _start_process(session,argv,str(program))


def _strict_zip_folder(archive: Path, folder: str, launcher: Path):
    with zipfile.ZipFile(archive) as z:
        files=[i.filename.replace('\\','/') for i in z.infolist() if not i.is_dir()]
        names=[i.filename.replace('\\','/').rstrip('/') for i in z.infolist()]
        if any(name != folder and not name.startswith(folder+'/') for name in names):
            raise RuntimeError('PORTABLE_ZIP_ROOT_MISMATCH')
        if (folder+'/'+launcher.as_posix()) not in files:
            raise RuntimeError('PORTABLE_ZIP_LAUNCHER_MISSING')


def _copy_persistent(old: Path, stage: Path):
    for name in PRESERVE:
        origin=old/name
        if not origin.exists():
            continue
        target=stage/name
        if origin.is_symlink() or target.is_symlink():
            raise RuntimeError('PORTABLE_STATE_CONTAINS_LINK')
        if origin.is_dir():
            if not _no_links(origin):
                raise RuntimeError('PORTABLE_STATE_CONTAINS_LINK')
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.copytree(origin,target,symlinks=False)
        elif origin.is_file():
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(origin,target)


def update_portable(options: dict, archive: Path, task_id: str, version: str,
                    root: Path, *, extractor, on_phase=None, base: Path=Path('C:/dsh'),
                    process_inspector=None, launcher=None) -> dict:
    """Stop only owned processes; keep old run-state after update or rollback."""
    profile=options.get('portable') or {}
    folder=profile.get('folder','')
    executable=safe_relative(profile.get('launcher',''))
    if executable.suffix.lower() not in ALLOWED_START or not VERSION.fullmatch(version):
        raise RuntimeError('PORTABLE_LAUNCHER_OR_VERSION_INVALID')
    version_file=profile.get('version_file','')
    if version_file:safe_relative(version_file)
    program=program_path(folder,base)
    if not program.is_dir() or not (program/executable).is_file():
        raise RuntimeError('PORTABLE_LOCAL_INSTALL_NOT_FOUND')
    if not _no_links(program):
        raise RuntimeError('PORTABLE_EXISTING_PROGRAM_CONTAINS_LINKS')
    if not base.is_dir():
        raise RuntimeError('PORTABLE_BASE_DIRECTORY_MISSING')
    if not re.fullmatch(r'[0-9a-f-]{36}',task_id):
        raise RuntimeError('PORTABLE_UPDATE_TASK_ID_INVALID')
    backup=base/('.vmo-backup-'+folder+'-'+task_id)
    stage=base/('.vmo-stage-'+task_id)
    extracted=root/'working'/('portable-'+task_id)
    failed=base/('.vmo-failed-'+folder+'-'+task_id)
    if any(x.exists() for x in (backup,stage,extracted,failed)):
        raise RuntimeError('PORTABLE_PREVIOUS_UPDATE_FILES_EXIST')
    checker=process_inspector or (lambda op: _processes(
        Path(__file__).with_name('Portable-Processes.ps1'),program,executable,op))
    launch=launcher or (lambda: _launch(program,executable))
    was_running=False
    swapped=False
    stopped=False
    def phase(name):
        if on_phase:on_phase(name)
    _strict_zip_folder(archive,folder,executable)
    extracted.parent.mkdir(parents=True,exist_ok=True)
    try:
        extracted.mkdir(exist_ok=False)
        extractor(archive,extracted)  # ZIP top folder stripped by existing safe extractor
        if not (extracted/executable).is_file() or not _no_links(extracted):
            raise RuntimeError('PORTABLE_EXTRACTED_CONTENT_INVALID')
        shutil.copytree(extracted,stage,symlinks=False)
        _copy_persistent(program,stage)
        if not (stage/executable).is_file() or not _no_links(stage):
            raise RuntimeError('PORTABLE_STAGE_VALIDATION_FAILED')
        phase('staged')
        old_ver=installed_version(program,executable.as_posix(),version_file)
        was_running=checker('Inspect')['running']
        if was_running:
            # A previously running application must be restartable in its
            # logged-in user session. Check this before altering any files.
            try:
                from .interactive_session import _get_active_session
            except ImportError:
                from interactive_session import _get_active_session
            if launcher is None:_get_active_session()
            checker('Stop')
            if checker('Inspect')['running']:
                raise RuntimeError('PORTABLE_PROGRAM_STILL_RUNNING')
            stopped=True
            phase('stopped')
        os.replace(program,backup)
        try:
            os.replace(stage,program)
            swapped=True
        except BaseException:
            os.replace(backup,program)
            raise
        phase('swapped')
        installed=installed_version(program,executable.as_posix(),version_file)
        # Check when both source and installed version are actually verifiable.
        if profile.get('version_source','').startswith('zip:') and installed['verified'] and installed['version'] != version:
            raise RuntimeError('PORTABLE_INSTALLED_VERSION_MISMATCH')
        if was_running:
            launch()
            healthy=False
            for _ in range(12):
                time.sleep(1)
                if checker('Inspect')['running']:
                    healthy=True;break
            if not healthy:
                raise RuntimeError('PORTABLE_RESTART_NOT_RUNNING')
            phase('healthy')
        else:
            phase('verified')
        return {'backup':str(backup),'previous_running':was_running,
                'restarted':was_running,'version':installed['version'],
                'version_source':installed['source'], 'runtime_version_api':
                    'version_file_or_exe' if installed['verified'] else 'not_verified',
                'old_version':old_ver['version'], 'version_evidence':
                    'installed_release_manifest' if installed['verified'] else 'unverified',
                'note':('升级完成并恢复此前运行状态' if was_running else '升级完成；原来未运行，保持关闭')}
    except BaseException as error:
        if swapped and backup.is_dir():
            try:
                if was_running and checker('Inspect')['running']:
                    checker('Stop')
                os.replace(program,failed)
                os.replace(backup,program)
                if was_running and stopped:
                    launch()
                phase('rolled_back_healthy')
            except BaseException as recovery_error:
                phase('recovery_required')
                raise RuntimeError('PORTABLE_ROLLBACK_REQUIRES_MANUAL_RECOVERY') from recovery_error
        elif stopped and was_running and program.is_dir():
            # Failure after stopping, before swap: resume original.
            try:launch()
            except BaseException:
                raise RuntimeError('PORTABLE_ORIGINAL_RESTART_FAILED') from error
        raise
    finally:
        if extracted.exists():shutil.rmtree(extracted,ignore_errors=True)
        if stage.exists():shutil.rmtree(stage,ignore_errors=True)
