"""Authenticated Host-only lifecycle diagnostics and safe restart handoff.

The detached PowerShell controller re-verifies both listener ports and the
health PID before terminating anything. It never terminates VMware guests.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path


class HostControlError(RuntimeError):
    pass


_SECRET_LINE = re.compile(r'(?i)\b(password|passwd|token|api[_-]?key|secret|authorization)\b\s*[:=]\s*\S+')
_SECRET_TOKEN = re.compile(r'(?i)(?:ghp_|github_pat_)[A-Za-z0-9_]{10,}|Bearer\s+[^\s]+')
_STATUS_STATES = frozenset(('verifying', 'stopping', 'starting'))


def _read_status(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding='utf-8-sig'))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _tail(path: Path, limit: int = 6000) -> str:
    if not path.is_file() or path.is_symlink():
        return ''
    try:
        with path.open('rb') as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - limit * 3))
            raw = fh.read(limit * 3)
        # Windows PowerShell log output can contain UTF-16 and OEM-encoded text.
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            text = raw.decode('utf-16', 'replace')
        elif raw.count(b'\x00') > len(raw) // 6:
            text = raw.decode('utf-16le', 'replace')
        else:
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = raw.decode('gb18030', 'replace')
        text = re.sub(r'\x1b\[[0-9;]*m', '', text)
        text = _SECRET_LINE.sub(lambda match: match.group(1) + '=[已隐藏]', text)
        text = _SECRET_TOKEN.sub('[已隐藏]', text)
        return text[-limit:]
    except OSError:
        return ''


class HostControl:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.work = self.root / 'data' / 'updates'
        self.status_file = self.work / 'host-control-status.json'
        self._mutex = threading.Lock()
        self.started_at = time.time()

    def status(self) -> dict:
        record = _read_status(self.status_file)
        # Never return arbitrary file paths or user-controlled file contents.
        return {'running': True, 'pid': os.getpid(), 'uptime_seconds': int(time.time() - self.started_at),
                'restart': {k: record[k] for k in
                            ('state', 'message', 'at', 'attempt', 'expected_pid', 'action')
                            if k in record}}

    def logs(self) -> dict:
        record = _read_status(self.status_file)
        attempt = record.get('attempt') if isinstance(record.get('attempt'), str) else ''
        allow_attempt = bool(re.fullmatch(r'[0-9a-fA-F]{32}', attempt))
        names = {
            'update_stdout': 'host-launch-out.log',
            'update_stderr': 'host-launch-error.log',
            'restart_audit': 'host-control.log',
        }
        if allow_attempt:
            names['restart_stdout'] = f'host-console-{attempt}-out.log'
            names['restart_stderr'] = f'host-console-{attempt}-err.log'
            names['restart_bootstrap'] = f'host-control-bootstrap-{attempt}.log'
        return {'logs': {label: _tail(self.work / filename)
                         for label, filename in names.items()},
                'restart': self.status()['restart']}

    def restart(self) -> dict:
        if os.name != 'nt':
            raise HostControlError('仅支持 Windows 宿主机重启控制')
        script = self.root / 'scripts' / 'host' / 'Control-VMOHost.ps1'
        if not script.is_file():
            raise HostControlError('缺少经过发布校验的 Host 控制脚本')
        with self._mutex:
            record = _read_status(self.status_file)
            if record.get('state') in _STATUS_STATES:
                age = time.time() - self.status_file.stat().st_mtime
                if age < 180:
                    raise HostControlError('管理程序已有一次重启正在进行，请先查看运行状态')
            self.work.mkdir(parents=True, exist_ok=True)
            attempt = uuid.uuid4().hex
            response = {'state': 'verifying', 'action': 'restart', 'attempt': attempt,
                        'expected_pid': os.getpid(), 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                        'message': '已接收重启请求，准备验证 Host 进程身份'}
            # Persist before spawning, so a second click cannot race.
            temp = self.work / ('host-control-' + attempt + '.tmp')
            temp.write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
            temp.replace(self.status_file)
            cmd = ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                   '-ExecutionPolicy', 'RemoteSigned', '-File', str(script),
                   '-HostPid', str(os.getpid()), '-Attempt', attempt]
            bootstrap = self.work / ('host-control-bootstrap-' + attempt + '.log')
            try:
                with bootstrap.open('wb') as fh:
                    subprocess.Popen(cmd, cwd=str(self.root), stdin=subprocess.DEVNULL,
                                     stdout=fh, stderr=subprocess.STDOUT,
                                     creationflags=(subprocess.CREATE_NO_WINDOW |
                                                    subprocess.CREATE_NEW_PROCESS_GROUP),
                                     close_fds=True)
            except OSError as exc:
                response['state'] = 'failed'
                response['message'] = '重启辅助进程未能启动：' + str(exc)[:150]
                self.status_file.write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
                raise HostControlError(response['message']) from exc
            return {'accepted': True, 'attempt': attempt, 'expected_pid': os.getpid()}
