"""Opt-in loopback-only VNC screenshot for VMware's built-in VNC server.

The VMware VMX is modified only while the managed guest is stopped. VNC auth
uses the legacy RFB/DES protocol, so it is *never* exposed to non-loopback.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import re
import secrets
import socket
import struct

from PIL import Image

from .vmrun import VmrunError

VNC_START, VNC_END = 5910, 6999
VNC_MAX_PIXELS = 3840 * 2160
SETTING_RE = re.compile(rb'^\s*remotedisplay\.vnc\.([\w]+)\s*=\s*"([^"]*)"\s*$', re.I)


def vnc_config(vmx: Path) -> dict:
    options = {}
    for line in vmx.read_bytes().splitlines():
        match = SETTING_RE.match(line)
        if match:
            options[match.group(1).decode('ascii').lower()] = match.group(2).decode('ascii', 'replace')
    return options


def _is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.15)
        return s.connect_ex(('127.0.0.1', port)) != 0


def enable_vnc(vmx: Path, used_ports: set[int]) -> dict:
    """Enables host-loopback VNC for one powered-off *managed clone* only."""
    cfg = vnc_config(vmx)
    if cfg.get('enabled', '').upper() == 'TRUE':
        if cfg.get('ip') != '127.0.0.1':
            raise VmrunError('Existing VNC configuration is not local-only; inspect VMX manually')
        raise VmrunError('VNC already enabled; no configuration changed')
    port = next((p for p in range(VNC_START, VNC_END + 1)
                 if p not in used_ports and _is_free(p)), None)
    if port is None:
        raise VmrunError('No available local VNC port')
    password = secrets.token_urlsafe(6)[:8]
    raw = vmx.read_bytes()
    eol = b'\r\n' if b'\r\n' in raw else b'\n'
    # Remove stale settings but retain unrelated VMX values and formatting.
    data = b''.join(line for line in raw.splitlines(keepends=True) if not SETTING_RE.match(line.strip()))
    if data and not data.endswith((b'\r', b'\n')):
        data += eol
    for key, value in [('enabled', 'TRUE'), ('ip', '127.0.0.1'),
                       ('port', str(port)), ('password', password)]:
        data += f'RemoteDisplay.vnc.{key} = "{value}"'.encode('ascii') + eol
    backup = vmx.with_suffix('.vmx.pre-vnc.bak')
    if backup.exists():
        raise VmrunError('VNC backup already exists; inspect before changing VMX again')
    backup.write_bytes(raw)
    tmp = vmx.with_suffix('.vmx.vnc.tmp')
    try:
        tmp.write_bytes(data)
        os.replace(tmp, vmx)
    finally:
        tmp.unlink(missing_ok=True)
    return {'enabled': True, 'port': port, 'ip': '127.0.0.1'}


def _recv(sock, count: int) -> bytes:
    parts = []
    while count:
        value = sock.recv(count)
        if not value:
            raise VmrunError('VNC server closed the connection')
        parts.append(value)
        count -= len(value)
    return b''.join(parts)


def _auth(sock, pwd: str, type_: int) -> None:
    if type_ == 2:
        challenge = _recv(sock, 16)
        # RFB/VNCAuth reverses the bit order of each DES key byte.
        def reverse(x):
            return int(f'{x:08b}'[::-1], 2)
        key = bytes(reverse(v) for v in pwd.encode('ascii')[:8].ljust(8, b'\x00'))
        try:
            from Crypto.Cipher import DES
        except ImportError as exc:
            raise VmrunError('VNC screenshot requires pycryptodome; install requirements.txt') from exc
        sock.sendall(DES.new(key, DES.MODE_ECB).encrypt(challenge))
    elif type_ != 1:
        raise VmrunError('Unsupported VNC security mechanism')


def capture_loopback_vnc(vmx: Path, *, preview: bool = False) -> bytes:
    cfg = vnc_config(vmx)
    if cfg.get('enabled', '').upper() != 'TRUE' or cfg.get('ip') != '127.0.0.1':
        raise VmrunError('请先在虚拟机关机时点击「启用本机截图」，再启动虚拟机')
    if not re.fullmatch(r'\d{4,5}', cfg.get('port', '')):
        raise VmrunError('VNC port missing or invalid')
    port = int(cfg['port'])
    if not VNC_START <= port <= VNC_END:
        raise VmrunError('VNC port is outside the managed local-only range')
    pwd = cfg.get('password', '')
    with socket.create_connection(('127.0.0.1', port), timeout=6) as sock:
        sock.settimeout(12)
        server_version = _recv(sock, 12)
        if not server_version.startswith(b'RFB 003.'):
            raise VmrunError('Unexpected VNC protocol version')
        # Version negotiation: VMware supports RFB 3.3/3.8.
        minor = int(server_version[8:11])
        version = b'RFB 003.008\n' if minor >= 7 else b'RFB 003.003\n'
        sock.sendall(version)
        if minor >= 7:
            n = _recv(sock, 1)[0]
            if n == 0:
                raise VmrunError('VNC server refused authentication')
            types = _recv(sock, n)
            if 2 in types and pwd:
                chosen = 2
            elif 1 in types:
                chosen = 1
            else:
                raise VmrunError('VNC authentication not supported')
            sock.sendall(bytes([chosen]))
            _auth(sock, pwd, chosen)
            ok = struct.unpack('>I', _recv(sock, 4))[0]
            if ok:
                raise VmrunError('VNC authentication failed')
        else:
            chosen = struct.unpack('>I', _recv(sock, 4))[0]
            _auth(sock, pwd, chosen)
            if chosen == 2 and struct.unpack('>I', _recv(sock, 4))[0]:
                raise VmrunError('VNC authentication failed')
        sock.sendall(b'\x01')
        width, height = struct.unpack('>HH', _recv(sock, 4))
        _recv(sock, 16)  # native pixel format
        name_bytes = struct.unpack('>I', _recv(sock, 4))[0]
        if not 0 < width <= 3840 or not 0 < height <= 2160 or width*height > VNC_MAX_PIXELS or name_bytes > 4096:
            raise VmrunError('VNC framebuffer size invalid')
        _recv(sock, name_bytes)
        # 32bpp BGRX little endian, true-color RGB888.
        pixfmt = struct.pack('>BBBBHHHBBBxxx', 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        sock.sendall(b'\x00\x00\x00\x00' + pixfmt)
        sock.sendall(struct.pack('>BBHi', 2, 0, 1, 0))  # SetEncodings RAW only
        sock.sendall(struct.pack('>BBHHHH', 3, 0, 0, 0, width, height))
        while True:
            msg = _recv(sock, 1)[0]
            if msg == 0:
                _recv(sock, 1)
                n = struct.unpack('>H', _recv(sock, 2))[0]
                if n > 256: raise VmrunError('Too many VNC rectangles')
                image = Image.new('RGB', (width, height))
                for _ in range(n):
                    x, y, w, h, encoding = struct.unpack('>HHHHi', _recv(sock, 12))
                    if encoding != 0 or x+w > width or y+h > height or w*h > VNC_MAX_PIXELS:
                        raise VmrunError('Unsupported VNC rectangle')
                    raw = _recv(sock, w*h*4)
                    rect = Image.frombytes('RGB', (w, h), raw, 'raw', 'BGRX')
                    image.paste(rect, (x, y))
                mem = io.BytesIO()
                if preview:
                    # One-shot thumbnail: no persistence, bounded bandwidth for 1 fps.
                    image.thumbnail((960, 540), Image.Resampling.LANCZOS)
                    image.save(mem, format='JPEG', quality=62, optimize=True)
                else:
                    image.save(mem, format='PNG')
                return mem.getvalue()
            if msg == 2:  # Bell
                continue
            if msg == 3:  # ServerCutText
                _recv(sock, 3)
                length = struct.unpack('>I', _recv(sock, 4))[0]
                if length > 1024*1024: raise VmrunError('VNC message too large')
                _recv(sock, length)
                continue
            raise VmrunError('Unexpected VNC server message')
