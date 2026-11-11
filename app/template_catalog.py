"""Safe read-only discovery of VMware templates under configured folder."""
from __future__ import annotations
from pathlib import Path
from .safety import _is_reparse_point


def discover_vmxtemplates(root: Path, registered: list[dict], limit: int = 500) -> list[dict]:
    root = root.resolve(strict=True)
    known = {str(Path(r['vmx_path']).resolve()).lower(): r for r in registered}
    found: list[dict] = []
    # Restrict to root and first level of subfolders (no unrelated nested assets).
    candidates = sorted(list(root.glob('*.vmx')) + list(root.glob('*/*.vmx')),
                        key=lambda path: str(path).lower())
    for item in candidates[:limit]:
        if _is_reparse_point(item) or _is_reparse_point(item.parent):
            continue
        try:
            resolved = item.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                continue
        except (OSError, ValueError):
            continue
        record = known.get(str(resolved).lower())
        found.append({'vmx_path': str(resolved), 'suggested_name': item.stem,
                      'registered': bool(record), 'template_id': record['id'] if record else None,
                      'status': record['status'] if record else 'UNREGISTERED'})
    return found


def find_linked_disk_references(vms_root: Path, template_folder: Path) -> list[str]:
    """Detect simple VMware linked-disk references to the template folder.

    Reads small text descriptors only; a binary descriptor requires manual
    verification. This is a conservative guard, not proof all disks are full.
    """
    from os import walk
    from pathlib import PureWindowsPath
    target = str(template_folder).replace('/', '\\').lower().rstrip('\\') + '\\'
    alerts = []
    if not vms_root.exists(): return alerts
    for base, dirs, files in walk(vms_root, followlinks=False):
        dirs[:] = [d for d in dirs if not _is_reparse_point(Path(base) / d) and not d.startswith('.')]
        for name in files:
            src = Path(base) / name
            if _is_reparse_point(src) or src.suffix.lower() not in ('.vmx', '.vmdk'):
                continue
            try:
                if src.stat().st_size > 1024 * 1024: continue
                data = src.read_bytes()
                if b'\x00' in data[:4096]: continue
                desc = data.decode('utf-8', errors='replace').lower().replace('/', '\\')
                # A direct absolute path is unsafe. Relative ../ references
                # across VM directory boundaries require further review.
                if target in desc or ('parentfilenamehint' in desc and '..\\' in desc):
                    alerts.append(str(src))
            except OSError:
                alerts.append(str(src))
            if len(alerts) >= 10: return alerts
    return alerts
