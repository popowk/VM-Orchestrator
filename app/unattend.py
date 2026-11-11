"""Per-clone OOBE answer media: no customer password or identity in golden VM.

Windows Setup may prefer an earlier cached Panther answer file. Consequently
attaching media is preparation, not proof that OOBE used it. Never report OOBE
success from a vmrun start or VMware Tools heartbeat alone.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import csv
import subprocess
import xml.etree.ElementTree as ET
from cryptography.fernet import Fernet, InvalidToken
from .vmrun import VmrunError

XMLNS = 'urn:schemas-microsoft-com:unattend'
WCM = 'http://schemas.microsoft.com/WMIConfig/2002/State'
ET.register_namespace('', XMLNS)
ET.register_namespace('wcm', WCM)
ARCH = {'processorArchitecture': 'amd64', 'publicKeyToken': '31bf3856ad364e35',
        'language': 'neutral', 'versionScope': 'nonSxS'}


def _tag(name): return '{%s}%s' % (XMLNS, name)

def _sub(parent, element_name, text=None, **attrs):
    element = ET.SubElement(parent, _tag(element_name), attrs)
    if text is not None: element.text = text
    return element


def make_hostname(vm_id: str) -> str:
    # Under Windows' 15-char NetBIOS computer-name limit, distinct for each VM.
    return 'VMO-' + re.sub(r'[^0-9a-f]', '', vm_id.lower())[:10].upper()


def make_xml(hostname: str, password: str, user: str = 'Administrator') -> bytes:
    if not re.fullmatch(r'[A-Za-z0-9-]{1,15}', hostname) or user != 'Administrator':
        raise ValueError('Unsafe Windows hostname/user')
    root = ET.Element(_tag('unattend'))
    spec = _sub(root, 'settings', pass_='specialize')
    spec.attrib['pass'] = spec.attrib.pop('pass_')
    shell = _sub(spec, 'component', **{'name': 'Microsoft-Windows-Shell-Setup', **ARCH})
    _sub(shell, 'ComputerName', hostname)
    oobe = _sub(root, 'settings', pass_='oobeSystem')
    oobe.attrib['pass'] = oobe.attrib.pop('pass_')
    locale = _sub(oobe, 'component', **{'name': 'Microsoft-Windows-International-Core', **ARCH})
    for field, value in [('InputLocale','0804:00000804'),('SystemLocale','zh-CN'),
                         ('UILanguage','zh-CN'),('UserLocale','zh-CN')]:
        _sub(locale, field, value)
    shell = _sub(oobe, 'component', **{'name': 'Microsoft-Windows-Shell-Setup', **ARCH})
    prefs = _sub(shell, 'OOBE')
    for field,value in [('HideOnlineAccountScreens','true'),('HideWirelessSetupInOOBE','true'),
                        ('HideOEMRegistrationScreen','true'),('ProtectYourPC','3')]:
        _sub(prefs, field, value)
    users = _sub(shell, 'UserAccounts')
    admin_pwd = _sub(users, 'AdministratorPassword')
    _sub(admin_pwd, 'Value', password)
    _sub(admin_pwd, 'PlainText', 'true')
    _sub(shell, 'TimeZone', 'China Standard Time')
    # One-time login: OOBE reaches the desktop without a human typing a password.
    # FirstLogon-OOBE.ps1 disables autologon and wipes Winlogon password state.
    auto = _sub(shell, 'AutoLogon')
    _sub(auto, 'Enabled', 'true')
    _sub(auto, 'Username', user)
    auto_pwd = _sub(auto, 'Password')
    _sub(auto_pwd, 'Value', password)
    _sub(auto_pwd, 'PlainText', 'true')
    _sub(auto, 'LogonCount', '1')
    commands = _sub(shell, 'FirstLogonCommands')
    cmd = _sub(commands, 'SynchronousCommand', **{'{%s}action' % WCM: 'add'})
    _sub(cmd, 'Order', '1')
    _sub(cmd, 'Description', 'Finalize VMO OOBE after desktop login')
    _sub(cmd, 'CommandLine', r'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\ProgramData\VMO-OOBE\FirstLogon-OOBE.ps1')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def make_iso(xml: bytes, destination: Path, bootstrap: dict | None = None, manifest: dict | None = None) -> None:
    try:
        import pycdlib
    except ImportError as exc:
        raise VmrunError('Creating unattended ISO requires pycdlib; install requirements.txt') from exc
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, vol_ident='VMO_INIT')
    try:
        source = io.BytesIO(xml)  # keep the source buffer alive until ISO has been mastered
        iso.add_fp(source, len(xml), iso_path='/AUTOUNAT.XML;1', joliet_path='/Autounattend.xml')
        if manifest:
            raw = json.dumps(manifest,ensure_ascii=True).encode('utf-8')
            manifest_stream = io.BytesIO(raw)
            iso.add_fp(manifest_stream,len(raw),iso_path='/VMODEV.JSON;1',joliet_path='/vmo-device.json')
        if bootstrap:
            payload = json.dumps({k:v for k,v in bootstrap.items() if k!='ca_pem'},ensure_ascii=True).encode('utf-8')
            agent_stream = io.BytesIO(payload)
            iso.add_fp(agent_stream,len(payload),iso_path='/VMOAGNT.JSON;1',joliet_path='/vmo-agent.json')
            ca_bytes = bootstrap['ca_pem'].encode('ascii')
            ca_stream = io.BytesIO(ca_bytes)
            iso.add_fp(ca_stream,len(ca_bytes),iso_path='/VMOCA.PEM;1',joliet_path='/vmo-ca.pem')
        iso.write(str(destination))
    finally:
        iso.close()


def _restrict_to_owner_and_system(path: Path) -> None:
    """Fail closed when Windows cannot restrict the cleartext answer ISO."""
    if os.name != 'nt':
        return
    try:
        r = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'],
                           capture_output=True, text=True, check=True, timeout=10)
        rows = list(csv.reader(io.StringIO(r.stdout)))
        sid = rows[0][-1].strip() if rows else ''
        if not re.fullmatch(r'S-1-5-[0-9-]+', sid):
            raise ValueError('Unable to resolve local Windows user SID')
        permission = subprocess.run(
            ['icacls', str(path), '/inheritance:r', '/grant:r',
             '*' + sid + ':(F)', '*S-1-5-18:(F)'],
            capture_output=True, text=True, check=False, timeout=20)
        if permission.returncode != 0:
            raise OSError('Windows file ACL could not be restricted')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise VmrunError('Cannot protect per-VM initialization media with Windows ACL; aborting') from exc


def _secret_cipher(admin_key: str) -> Fernet:
    if not admin_key or len(admin_key) < 16:
        raise VmrunError('Host admin key is too weak to encrypt unattended credentials')
    # Only the existing DPAPI-protected host key can unlock this secret.
    derived = hashlib.sha256(b'vmo-v0.7-guest-credentials\x00' + admin_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def read_secret(secret_dir: Path, vm_id: str, admin_key: str) -> dict | None:
    path = secret_dir / (vm_id + '.secret')
    if not path.exists(): return None
    try:
        value = _secret_cipher(admin_key).decrypt(path.read_bytes()).decode('ascii')
        username, hostname, password = value.split('|', 2)
        return {'username': username, 'hostname': hostname, 'password': password}
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise VmrunError('Unable to decrypt provision secret; original Host Agent admin key required') from exc


def provisioning_status(secret_dir: Path, vm_id: str, vmx: Path) -> dict:
    prepared = (secret_dir / (vm_id + '.secret')).is_file()
    raw = vmx.read_text(encoding='utf-8', errors='replace') if vmx.is_file() else ''
    return {
        'prepared': prepared,
        'media_attached': 'vmo-init.iso' in raw.lower(),
        'oobe_verified': False,
        'bootstrap_required': True,
        'note': '仅挂载 ISO 不足以保证自动 OOBE；模板须使用 V1.0 specialize 引导并实机通过 Guest Agent 验证。',
    }


def prepare_clone(vmx: Path, vm_id: str, secret_dir: Path, admin_key: str, bootstrap: dict | None = None) -> dict:
    """Called ONLY on a brand-new powered-off clone before first start."""
    if not vmx.is_file() or vmx.is_symlink():
        raise VmrunError('Unable to prepare missing/unsafe clone VMX')
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret_file = secret_dir / (vm_id + '.secret')
    if secret_file.exists():
        raise VmrunError('Clone provisioning secret already exists; refusing to rotate it')
    iso_path = vmx.parent / 'vmo-init.iso'
    if iso_path.exists():
        raise VmrunError('Provisioning ISO already exists; refusing to overwrite')
    hostname = make_hostname(vm_id)
    username = 'Administrator'
    password = secrets.token_urlsafe(27)  # independent high-entropy per VM
    old = vmx.read_bytes()
    match = set()
    for line in old.splitlines():
        m = re.match(rb'^\s*([a-zA-Z0-9_.:]+)\s*=', line)
        if m: match.add(m.group(1).lower().decode('ascii'))
    slot = next((s for s in ['sata0:1', 'sata0:2', 'sata1:0', 'sata1:1', 'ide1:1']
                 if not any(k.startswith(s + '.') for k in match)), None)
    if not slot:
        raise VmrunError('No free virtual CD/DVD slot in clone VMX')
    # An ISO contains a per-VM cleartext bootstrap password. Treat as a secret.
    xml = make_xml(hostname, password, username)
    try:
        manifest = {'schema': 1, 'vm_id': vm_id, 'hostname': hostname, 'username': username}
        make_iso(xml, iso_path, bootstrap, manifest=manifest)
        _restrict_to_owner_and_system(iso_path)
        eol = b'\r\n' if b'\r\n' in old else b'\n'
        additions = []
        if slot.startswith('sata'):
            ctl = slot.split(':', 1)[0] + '.present'
            if ctl not in match:
                additions.append(f'{ctl} = "TRUE"')
        additions += [f'{slot}.present = "TRUE"',
                      f'{slot}.deviceType = "cdrom-image"',
                      f'{slot}.fileName = "{iso_path}"',
                      f'{slot}.startConnected = "TRUE"']
        extra = eol.join(line.encode('utf-8') for line in additions) + eol
        if old and not old.endswith((b'\n', b'\r')): old += eol
        tmp = vmx.with_suffix('.vmx.unattend.tmp')
        try:
            tmp.write_bytes(old + extra)
            os.replace(tmp, vmx)
        finally:
            tmp.unlink(missing_ok=True)
        payload = '|'.join([username, hostname, password]).encode('ascii')
        token = _secret_cipher(admin_key).encrypt(payload)
        with secret_file.open('xb') as f:
            f.write(token)
        _restrict_to_owner_and_system(secret_file)
        try: os.chmod(secret_file, 0o600)
        except OSError: pass
    except Exception:
        # Do not leave a plaintext ISO or partially written host secret.
        iso_path.unlink(missing_ok=True)
        secret_file.unlink(missing_ok=True)
        raise
    return {'username': username, 'hostname': hostname, 'media_attached': True}


def cleanup_clone_media(vmx: Path, secret_dir: Path, vm_id: str) -> None:
    """Detach and remove only the exact program-created ISO while powered off.

    Retain the host-encrypted credential so the owner can retrieve the local
    guest account after the media is gone. Guest Panther copies are not removed.
    """
    if not (secret_dir / (vm_id + '.secret')).is_file():
        raise VmrunError('No known provisioning secret; refusing to remove unrelated CD media')
    iso_path = vmx.parent / 'vmo-init.iso'
    original = vmx.read_bytes()
    lines = original.splitlines(keepends=True)
    # Detect the exact device attached to our ISO; never modify a different drive.
    target = re.escape(str(iso_path).encode('utf-8'))
    dev = None
    for line in lines:
        m = re.match(rb'^\s*((?:ide|sata)\d+:\d+)\.fileName\s*=\s*"' + target + rb'"\s*$', line.strip(), re.I)
        if m:
            dev = m.group(1).lower()
            break
    if dev is None:
        raise VmrunError('Provisioning ISO not attached to expected VMX slot')
    pattern = re.compile(rb'^\s*' + re.escape(dev) + rb'\.(?:present|deviceType|fileName|startConnected)\s*=', re.I)
    cleaned = b''.join(line for line in lines if not pattern.match(line))
    if cleaned == original:
        raise VmrunError('No matching media settings to clean')
    temp = vmx.with_suffix('.vmx.detach.tmp')
    try:
        temp.write_bytes(cleaned)
        os.replace(temp, vmx)
    finally:
        temp.unlink(missing_ok=True)
    iso_path.unlink(missing_ok=True)
