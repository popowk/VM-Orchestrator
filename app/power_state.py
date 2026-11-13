"""Conservative VMware Workstation power-state evidence.

vmrun listRunningVms verifies a currently running VM, not whether its last
suspension is still active. A *.vmss file by itself might be left over after
a shutdown or old operation. The VMX checkpoint.vmState reference is the
stronger indication that VMware is configured to resume a saved state.
Never delete/mutate vmss, vmx or lck files from this module.
"""
from __future__ import annotations

from pathlib import Path
import re

_STATE = re.compile(r'^\s*checkpoint\.vmState\s*=\s*"([^"]*)"', re.I | re.M)


def suspend_evidence(vmx: Path) -> dict:
    try:
        text = vmx.read_text(encoding='utf-8-sig', errors='replace')[:2_000_000]
    except OSError:
        return {'state': 'unknown', 'checkpoint': '', 'suspend_files': [],
                'warning': '无法读取 VMX 的 checkpoint.vmState；请在 VMware Workstation 中核实'}
    match = _STATE.search(text)
    value = (match.group(1).strip() if match else '')
    suspended = sorted(x.name for x in vmx.parent.glob('*.vmss') if x.is_file())
    if value:
        checkpoint = Path(value.replace('\\', '/')).name
        # A referenced checkpoint may be in VMware's working directory.
        # A missing referenced file is NOT evidence that shutdown succeeded.
        exists = (vmx.parent / checkpoint).is_file() if checkpoint else False
        return {'state': 'suspended' if exists else 'unverified',
                'checkpoint': value, 'suspend_files': suspended,
                'warning': ('VMX 指向保存的挂起内存：' + checkpoint
                            if exists else
                            'VMX 仍引用挂起状态，但保存文件不可用；需要 VMware 检查，禁止重装')}
    return {'state': 'residual' if suspended else 'clear',
            'checkpoint': '', 'suspend_files': suspended,
            'warning': ('VMX 没有挂起引用，但发现残留 .vmss；显示已关机仅代表 vmrun 未运行，'
                        '重装和磁盘修改仍被保护。请备份后在 VMware 中确认残留文件状态。'
                        if suspended else '')}


def reported_power_state(vmx: Path, running: bool | None) -> str:
    if running is None:
        return 'UNKNOWN'
    if running:
        return 'POWERED_ON'
    state = suspend_evidence(vmx)['state']
    if state in ('suspended',):
        return 'SUSPENDED'
    if state in ('unverified', 'unknown'):
        return 'UNKNOWN'
    return 'POWERED_OFF'
