"""Admin-requested DSH plugin command in logged-in Windows user profile.

The host/Gateway accepts only one vetted DSH plugin statement (no general
PowerShell).  This is not a way to install into the SYSTEM account.
"""
from __future__ import annotations
import base64
import os
from pathlib import Path
import shutil
import subprocess
import uuid
try:
    from .user_update_exec import active_user_sid,run_as_user
except ImportError:
    from user_update_exec import active_user_sid,run_as_user


def execute_plugin_command(command: str, version: str, root:Path, on_phase=None):
    if os.name!='nt':raise RuntimeError('WINDOWS_ONLY')
    # Validate independently in Guest as well as the administrator API.
    try:from .update_commands import validate_plugin_command
    except ImportError:from update_commands import validate_plugin_command
    validate_plugin_command(command,version)
    sid=active_user_sid()
    # Best-effort profile/data backup prior to running. A missing copy refuses
    # the update only when the configured profile directory exists.
    backup=''
    src=Path(r'C:\dsh\.dsh')
    if src.is_dir():
        folder=root/'backups'/('dsh-plugin-'+uuid.uuid4().hex)
        folder.parent.mkdir(parents=True,exist_ok=True)
        shutil.copytree(src,folder,symlinks=False)
        backup=str(folder)
    if on_phase:on_phase('staged')
    pwsh=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
    script="$ErrorActionPreference='Stop'; "+command+"; if($null -ne $LASTEXITCODE){exit [int]$LASTEXITCODE}else{exit 0}"
    encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
    outcome=run_as_user(str(pwsh),['-NoProfile','-NonInteractive','-EncodedCommand',encoded],sid,timeout=900)
    if outcome['exit_code']!=0:raise RuntimeError('DSH_PLUGIN_COMMAND_FAILED')
    if on_phase:on_phase('healthy')
    return {'version_requested':version,'exit_code':0,'user_sid':sid,'backup':backup,
            'version_evidence':'command_exit_only',
            'note':'DSH plugin PowerShell command exited 0 under logged-on user; exact installed plugin version still requires a separate list/functional check'}
