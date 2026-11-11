"""Strict subprocess adapter for the installed VMware Workstation vmrun.exe.

No shell interpolation, arbitrary commands, or supplied executable paths are exposed
through HTTP. This adapter must run on the real Windows VMware host.
"""
from pathlib import Path
import re
import subprocess


class VmrunError(RuntimeError):
    pass


class Vmrun:
    def __init__(self, executable: Path):
        self.executable = executable

    def _run(self, *args: str, timeout: int = 120) -> str:
        if not self.executable.is_file():
            raise VmrunError('vmrun.exe not found on the host')
        try:
            p = subprocess.run(
                [str(self.executable), '-T', 'ws', *args],
                capture_output=True, text=True, errors='replace', timeout=timeout,
                check=False, shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VmrunError('vmrun operation timed out; inspect VMware before retrying') from exc
        except OSError as exc:
            raise VmrunError('Unable to launch vmrun.exe') from exc
        if p.returncode:
            # Limit exposure of paths and guest details in CLI stdout/stderr.
            raise VmrunError(f'vmrun failed (exit code {p.returncode}); inspect host VMware logs')
        return p.stdout

    def list_running(self) -> list[Path]:
        lines = self._run('list', timeout=30).splitlines()
        if not lines or not lines[0].lower().startswith('total running vms:'):
            raise VmrunError('Unexpected vmrun list response')
        return [Path(line.strip().strip('"')) for line in lines[1:] if line.strip()]

    def is_running(self, vmx_path: Path) -> bool:
        requested = str(vmx_path.resolve()).casefold()
        return any(str(item.resolve()).casefold() == requested for item in self.list_running())

    def clone_full(self, src: Path, dst: Path, name: str) -> None:
        # Clone support differs across Workstation versions; failure is NOT silently
        # replaced by a disk-folder copy, which can preserve MACs/UUIDs/references.
        self._run('clone', str(src), str(dst), 'full', f'-cloneName={name}', timeout=3600)

    def start(self, vmx: Path) -> None:
        self._run('start', str(vmx), 'nogui', timeout=180)

    def stop(self, vmx: Path, mode: str = 'soft') -> None:
        if mode not in {'soft', 'hard'}:
            raise ValueError('Invalid power mode')
        self._run('stop', str(vmx), mode, timeout=180)

    def snapshots(self, vmx: Path) -> list[str]:
        output = self._run('listSnapshots', str(vmx), timeout=60)
        lines = output.splitlines()
        if not lines or not re.match(r'^Total snapshots:\s*\d+', lines[0].strip(), re.I):
            raise VmrunError('Unexpected vmrun listSnapshots response')
        return [line.strip() for line in lines[1:] if line.strip()]

    def snapshot_create(self, vmx: Path, name: str) -> None:
        self._run('snapshot', str(vmx), name, timeout=1200)

    def snapshot_restore(self, vmx: Path, name: str) -> None:
        self._run('revertToSnapshot', str(vmx), name, timeout=1200)

    def snapshot_delete(self, vmx: Path, name: str) -> None:
        self._run('deleteSnapshot', str(vmx), name, timeout=1200)

    def check_tools_state(self, vmx: Path) -> str:
        return self._run('checkToolsState', str(vmx), timeout=15).strip().lower()

    def guest_ip(self, vmx: Path) -> str | None:
        value = self._run('getGuestIPAddress', str(vmx), timeout=15).strip()
        # No -wait: never block during Windows OOBE.
        import ipaddress
        try:
            addr = ipaddress.ip_address(value)
        except ValueError:
            return None
        return str(addr) if not addr.is_loopback and not addr.is_link_local else None
