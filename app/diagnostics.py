"""Read-only VMware power and lock diagnostics.

A *.lck marker is not proof that the guest is powered on. We never delete it.
Offline VMX edits are allowed only when vmrun confirms OFF and the VMX passes
an OS-level exclusive-open probe. Suspended VMs always fail closed.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
from .vmrun import Vmrun, VmrunError


def exclusive_open_possible(path: Path) -> bool:
    """On Windows, test Windows sharing semantics without modifying VMX."""
    if os.name == 'nt':
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        fn = k32.CreateFileW
        fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        fn.restype = wintypes.HANDLE
        # GENERIC_READ, sharing=0 (exclusive), OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
        handle = fn(str(path), 0x80000000, 0, None, 3, 0x80, None)
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid or handle is None:
            return False
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle(handle)
        return True
    # A unit-test/non-Windows diagnostic is not a reliable Windows lock probe.
    return path.is_file() and os.access(path, os.R_OK)


def diagnose(vmx: Path, vmrun: Vmrun, *, probe=exclusive_open_possible) -> dict:
    try:
        running = bool(vmrun.is_running(vmx))
        power_verified = True
    except VmrunError:
        running = False
        power_verified = False
    locks = sorted(x.name for x in vmx.parent.glob('*.lck'))
    suspend = sorted(x.name for x in vmx.parent.glob('*.vmss'))
    # No exclusive-open probe is attempted for suspended/running/unknown state.
    # Distinguish "skipped for safety" from a real file lock failure.
    probe_skipped = not power_verified or bool(running) or bool(suspend)
    exclusive = bool(probe(vmx)) if not probe_skipped else False
    power = 'UNKNOWN' if not power_verified else ('POWERED_ON' if running else ('SUSPENDED' if suspend else 'POWERED_OFF'))
    if not power_verified:
        cause = 'VMware vmrun 无法确认电源状态；禁止修改 VMX'
    elif running:
        cause = '虚拟机仍在运行；关机后才能修改截图配置'
    elif suspend:
        cause = 'VMware 未报告正在运行，但发现 .vmss 保存的挂起内存；应按挂起处理。请点击恢复运行，再在 Windows 内正常关机后重新诊断；禁止手工删除 .vmss 或 .lck'
    elif not exclusive:
        cause = 'VMX 文件当前可能被 VMware 或其他进程占用；请关闭该虚拟机的设置窗口后重试'
    elif locks:
        cause = 'vmrun 确认已关机；存在锁标记但 VMX 可独占打开。可能是残留锁，不会自动删除'
    else:
        cause = '已关机并可安全尝试修改 VMX'
    return {
        'vmx_exists': vmx.is_file(), 'power_state': power,
        'vmrun_confirmed': power_verified, 'lock_markers': locks,
        'suspend_files': suspend, 'vmx_exclusive_open': exclusive,
        'vmx_probe_skipped': probe_skipped,
        'offline_vmx_edit_possible': power == 'POWERED_OFF' and exclusive,
        'reason': cause,
        'warning': ('发现 .lck 标记；VMware 管理界面仍可能修改配置，请避免同时打开设置窗口。' if locks else None),
    }
