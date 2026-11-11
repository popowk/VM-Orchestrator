"""Bind a registered Guest VM ID to its Windows VMware SMBIOS UUID.

Protects against accidentally cloning an already registered Guest or reusing a
registered agent.json on another VM. Does NOT replace gateway identity checks.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess


_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def current_uuid() -> str:
    if os.name != 'nt':
        raise RuntimeError('Windows Guest expected')
    ps = ('(Get-CimInstance Win32_ComputerSystemProduct -ErrorAction Stop).UUID')
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',ps],
                            capture_output=True,text=True,timeout=20,check=True)
    lines=[s.strip().lower() for s in result.stdout.splitlines() if s.strip()]
    value=lines[-1] if lines else ''
    if not _UUID.fullmatch(value) or value in ('00000000-0000-0000-0000-000000000000',
                                               'ffffffff-ffff-ffff-ffff-ffffffffffff'):
        raise RuntimeError('Invalid Guest SMBIOS UUID')
    return value


def verify_binding_value(binding: dict, vm_id: str, uuid: str) -> None:
    if binding.get('vm_id') != vm_id or binding.get('smbios_uuid','').lower() != uuid.lower():
        raise PermissionError('Registered VM identity differs from this Windows installation')


def verify_machine_binding(state: Path, vm_id: str) -> str:
    value=current_uuid()
    path=state/'machine-binding.json'
    if path.exists():
        verify_binding_value(json.loads(path.read_text(encoding='utf-8-sig')),vm_id,value)
        return value
    # Configuration and gateway-authenticated identity already match. First
    # successful connection pins the current VMware BIOS UUID to this VM ID.
    from uuid import uuid4
    tmp=state/('machine-binding-'+uuid4().hex+'.tmp')
    try:
        tmp.write_text(json.dumps({'vm_id':vm_id,'smbios_uuid':value},indent=2),encoding='utf-8')
        os.replace(tmp,path)
    finally:
        if tmp.exists(): tmp.unlink()
    return value
