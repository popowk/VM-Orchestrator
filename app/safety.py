"""Fail-closed guards for destructive VMware Workstation operations."""
from __future__ import annotations
import os
from pathlib import Path
from .diagnostics import exclusive_open_possible
from .vmrun import Vmrun, VmrunError

MAX_SNAPSHOTS = 3


def assert_powered_off(vmx: Path, vmrun: Vmrun, *, strict_locks: bool = False) -> None:
    """Require confirmed OFF (not suspended). Snapshot operations tolerate stale .lck.

    Destructive VM folder removal additionally rejects any lock marker and requires
    exclusive access to the VMX. This prevents unsafe removal while VMware owns it.
    """
    try:
        running = vmrun.is_running(vmx)
    except VmrunError as exc:
        raise VmrunError('Cannot verify VMware power state; operation denied') from exc
    if running:
        raise VmrunError('VM must be powered off before this operation')
    if any(vmx.parent.glob('*.vmss')):
        raise VmrunError('VM is suspended; resume and shut it down normally before this operation')
    if strict_locks:
        if any(vmx.parent.glob('*.lck')):
            raise VmrunError('VM has VMware lock markers; close VMware VM console/settings and diagnose before deletion')
        if vmx.is_file() and not exclusive_open_possible(vmx):
            raise VmrunError('VMX is held by another process; deletion denied')


def _is_reparse_point(p: Path) -> bool:
    if p.is_symlink():
        return True
    if hasattr(os.path, 'isjunction') and os.path.isjunction(p):
        return True
    try:
        st = os.stat(p, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return bool(getattr(st, 'st_file_attributes', 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def assert_safe_vm_delete_dir(vmx: Path, vms_root: Path, *, allow_missing_vmx: bool = False) -> None:
    """Only remove a direct child of the configured VMs root, never junctions/links.

    The filename must match the folder and be a managed .vmx. The caller checks
    its corresponding DB record separately. We deliberately do not follow links.
    """
    root = vms_root.resolve(strict=True)
    folder = vmx.parent
    if folder.parent.resolve() != root or folder.name in {'.', '..', ''}:
        raise VmrunError('Refusing deletion outside the managed VMs root')
    if vmx.name != folder.name + '.vmx':
        raise VmrunError('VMX/folder name mismatch; refusing deletion')
    if folder.exists() and _is_reparse_point(folder):
        raise VmrunError('VM folder is a junction/link; refusing deletion')
    if not folder.is_dir():
        if allow_missing_vmx and not folder.exists():
            return  # Deleting a stranded ERROR record with no remaining directory
        raise VmrunError('VM folder is missing or not a directory')
    if folder.resolve(strict=True).parent != root:
        raise VmrunError('VM folder escapes managed root')
    if vmx.exists():
        if _is_reparse_point(vmx) or not vmx.is_file():
            raise VmrunError('VMX is unsafe to delete')
    elif not allow_missing_vmx:
        raise VmrunError('VMX missing; cannot delete READY VM')
    # A cloned VM normally has no reparse points. If one exists, stop rather
    # than removing an entry that may point to another customer's data.
    for base, dirs, files in os.walk(folder, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(base) / name
            if _is_reparse_point(path):
                raise VmrunError('VM directory contains a symlink/junction; delete refused')
