"""VMO Guest Agent V2 runtime utilities. No V1 scheduled task or polling loop.

Does not run arbitrary host-provided commands. Software updates require locally
a Host-reviewed single-folder C-dsh ZIP catalog and SHA-256 matching.
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
        # The required portable ZIP wraps files in its single program folder.
        strip = len({p.parts[0] for p in names})==1 and all(len(p.parts)>1 for p in names)
        for i,p in zip(infos,names):
            rel = Path(*p.parts[1:]) if strip else p
            if not rel.parts: continue
            dst=dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.resolve().is_relative_to(dest.resolve()): raise RuntimeError('Unsafe resolved destination')
            with z.open(i) as inp, dst.open('wb') as out:
                shutil.copyfileobj(inp,out)


def safe_update_failure(exc: Exception) -> str:
    """Allow only fixed, non-secret error codes from the portable updater."""
    value=str(exc).split(';',1)[0].strip()
    permitted={
        'LEGACY_UPDATE_DISABLED','LEGACY_ROLLBACK_DISABLED',
        'PORTABLE_LOCAL_INSTALL_NOT_FOUND','PORTABLE_BASE_DIRECTORY_MISSING',
        'PORTABLE_EXISTING_PROGRAM_CONTAINS_LINKS','PORTABLE_PROGRAM_FOLDER_INVALID',
        'PORTABLE_ZIP_ROOT_MISMATCH','PORTABLE_ZIP_LAUNCHER_MISSING',
        'PORTABLE_EXTRACTED_CONTENT_INVALID','PORTABLE_STAGE_VALIDATION_FAILED',
        'PORTABLE_INSTALLED_VERSION_MISMATCH','PORTABLE_PROCESS_INSPECTION_OR_STOP_FAILED',
        'PORTABLE_RESTART_NOT_RUNNING','PORTABLE_ROLLBACK_REQUIRES_MANUAL_RECOVERY',
        'PORTABLE_ORIGINAL_RESTART_FAILED','PORTABLE_PROGRAM_STILL_RUNNING',
        'PORTABLE_PREVIOUS_UPDATE_FILES_EXIST','PORTABLE_PROCESS_RESULT_INVALID',
        'PORTABLE_WINDOWS_GUEST_ONLY','PORTABLE_PROCESS_HELPER_NOT_INSTALLED',
        'PORTABLE_LAUNCHER_OR_VERSION_INVALID','PORTABLE_UPDATE_TASK_ID_INVALID'
    }
    return value if value in permitted else type(exc).__name__


def perform_update(task: dict, config: dict, root: Path, ctx, on_phase=None):
    """Only immutable, pinned C:\dsh single-directory ZIP releases are accepted."""
    options=task.get('options') or {}
    if (task.get('kind')!='UPDATE' or
        not re.fullmatch(r'portable-p-[0-9a-f]{20}',task.get('product','')) or
        not isinstance(options,dict) or options.get('mode')!='portable_catalog'):
        raise RuntimeError('LEGACY_UPDATE_DISABLED')
    version=task.get('version','')
    if not version or len(version)>40 or not all(x.isalnum() or x in '.-_+' for x in version):
        raise RuntimeError('PORTABLE_LAUNCHER_OR_VERSION_INVALID')
    headers={'X-Agent-ID':config['agent_id'],'X-Agent-Key':config['agent_key']}
    content=api(config['server_url'],f"/agent/artifacts/{task['package_id']}",
                headers=headers,ssl_context=ctx,binary=True)
    if on_phase:on_phase('downloaded')
    if hashlib.sha256(content).hexdigest().lower()!=task['sha256'].lower():
        raise RuntimeError('Package SHA-256 mismatch')
    root.mkdir(parents=True,exist_ok=True)
    archive=root/('download-'+task['id']+'.zip')
    archive.write_bytes(content)
    try:
        try:from .portable_software import update_portable
        except ImportError:from portable_software import update_portable
        result=update_portable(options,archive,task['id'],version,root,
                               extractor=safe_extract_zip,on_phase=on_phase)
    finally:
        archive.unlink(missing_ok=True)
    if on_phase:on_phase('verified')
    # Guest portable app's reported version is computed from real filesystem
    # evidence; don't write the requested version as if independently confirmed.
    return result


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



def _safe_portable_inventory():
    if os.name!='nt':
        return {}
    try:
        from .portable_software import scan_c_dsh
    except ImportError:
        from portable_software import scan_c_dsh
    try:
        return scan_c_dsh()
    except (OSError, ValueError, RuntimeError):
        return {}

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
            'guest_agent':json.loads((Path(__file__).with_name('agent-release.json')).read_text(encoding='utf-8'))['version'],'oobe':oobe_evidence(config),
            'install_modes':{k:v.get('mode','disabled') for k,v in config.get('installations',{}).items()},
            'detected_software':detected_installations(config),
            'portable_software':_safe_portable_inventory(),
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
