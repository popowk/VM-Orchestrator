"""Explicit, fail-closed maintenance of disposable VMO update files and
registered, completed reinstall backups. Never scan arbitrary customer files.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from datetime import datetime, timezone
from .safety import _is_reparse_point, assert_powered_off
from .template_catalog import find_linked_disk_references


class CleanupError(ValueError):
    pass


ACTIVE_UPDATE = frozenset({
    'downloading', 'verified', 'handoff', 'hot_applying', 'preparing',
    'stopping', 'backing_up', 'applying', 'restarting',
})


def _guard_tree(path: Path, expected_parent: Path) -> tuple[int, int]:
    """Reject junctions, links, missing paths, escaped dirs and huge trees."""
    if (_is_reparse_point(expected_parent) or _is_reparse_point(path) or
            not path.is_dir() or path.parent != expected_parent or
            expected_parent.resolve(strict=True) != path.resolve(strict=True).parent):
        raise CleanupError('Directory is outside its managed parent or uses a reparse point')
    total = count = 0
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            child = Path(base) / name
            if _is_reparse_point(child):
                raise CleanupError('Directory contains a link or junction')
            if child.is_file():
                total += child.stat().st_size
                count += 1
                if count > 100000:
                    raise CleanupError('Too many files; inspect manually')
    return total, count


def _entry(item_id: str, kind: str, title: str, path: Path,
           bytes_size: int, files: int, mtime: float, manual: bool) -> dict:
    material = json.dumps([item_id, str(path), bytes_size, files, mtime],
                          ensure_ascii=False, separators=(',', ':'))
    return {
        'id': item_id, 'kind': kind, 'title': title, 'path': str(path),
        'bytes': bytes_size, 'files': files, 'mtime': mtime,
        'manual': manual, 'fingerprint': hashlib.sha256(material.encode()).hexdigest(),
    }


class CleanupManager:
    def __init__(self, root: Path, vms_root: Path, db_path: Path, store, fleet, vmrun):
        self.root = root.resolve(strict=True)
        self.vms_root = vms_root.resolve(strict=True)
        self.db_path = Path(db_path)
        self.store = store
        self.fleet = fleet
        self.vmrun = vmrun

    def _check_idle(self):
        with self.store._conn() as d:
            if d.execute("SELECT 1 FROM jobs WHERE status IN ('QUEUED','RUNNING') LIMIT 1").fetchone():
                raise CleanupError('VMware task is active; cleanup is locked')
        with self.fleet.db() as d:
            if d.execute("SELECT 1 FROM guest_tasks WHERE status IN ('QUEUED','CLAIMED','RUNNING') LIMIT 1").fetchone():
                raise CleanupError('Guest update task is active; cleanup is locked')
        status_file = self.root / 'data' / 'updates' / 'status.json'
        try:
            status = json.loads(status_file.read_text(encoding='utf-8-sig'))
        except (FileNotFoundError, ValueError):
            status = {}
        if status.get('state') in ACTIVE_UPDATE:
            raise CleanupError('Host update is active; cleanup is locked')

    def scan(self) -> dict:
        self._check_idle()
        items, protected = [], []
        now = time.time()
        updates = self.root / 'data' / 'updates'
        if updates.is_dir() and not _is_reparse_point(updates):
            status_file = updates / 'status.json'
            try:
                status = json.loads(status_file.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                status = {}
            latest_attempt = str(status.get('attempt') or '')
            # Cache: only exact release staging directories, older than seven days.
            for candidate in sorted(updates.iterdir()):
                name = candidate.name
                if not re.fullmatch(r'(?:stage|unpacked)-v\d+\.\d+\.\d+', name):
                    continue
                try:
                    if now - candidate.stat().st_mtime < 7 * 86400:
                        continue
                    total, files = _guard_tree(candidate, updates)
                    items.append(_entry('stage:' + name, 'program_cache',
                                        '旧更新临时目录', candidate, total, files,
                                        candidate.stat().st_mtime, False))
                except (OSError, CleanupError) as exc:
                    protected.append({'path': str(candidate), 'reason': str(exc)})
            # Log cleanup never deletes the latest updater attempt.
            for candidate in sorted(updates.glob('updater-*.log')):
                if not re.fullmatch(r'updater-[0-9a-fA-F]{32}\.log', candidate.name):
                    continue
                if candidate.stem == 'updater-' + latest_attempt:
                    continue
                try:
                    if (_is_reparse_point(candidate) or not candidate.is_file() or
                            now - candidate.stat().st_mtime < 30 * 86400):
                        continue
                    items.append(_entry('log:' + candidate.name, 'old_log',
                                        '旧更新日志', candidate, candidate.stat().st_size, 1,
                                        candidate.stat().st_mtime, False))
                except OSError:
                    continue
            backup_root = updates / 'backups'
            if backup_root.is_dir() and not _is_reparse_point(backup_root):
                names = sorted((x for x in backup_root.iterdir()
                                if re.fullmatch(r'(?:before|hot)-v\d+\.\d+\.\d+-[A-Za-z0-9-]+', x.name)),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                # Keep the two newest rollback archives, unconditionally.
                for candidate in names[2:]:
                    try:
                        if now - candidate.stat().st_mtime < 30 * 86400:
                            continue
                        total, files = _guard_tree(candidate, backup_root)
                        items.append(_entry('code:' + candidate.name, 'code_backup',
                                            '旧版本代码备份（手动选择）', candidate,
                                            total, files, candidate.stat().st_mtime, True))
                    except (OSError, CleanupError) as exc:
                        protected.append({'path': str(candidate), 'reason': str(exc)})
        backup_root = self.vms_root / '.vmo-reinstall-backups'
        if backup_root.is_dir() and not _is_reparse_point(backup_root):
            with self.fleet.db() as d:
                rows = [dict(row) for row in d.execute(
                    "SELECT job_id,vm_id,backup_path,status,backup_deleted_at "
                    "FROM vm_reinstalls WHERE status='SUCCEEDED' AND "
                    "backup_deleted_at IS NULL AND backup_path IS NOT NULL ORDER BY completed_at DESC")]
            for row in rows[:1000]:
                path = Path(row['backup_path'])
                vm = self.store.get_vm(row['vm_id'])
                expected = backup_root / ((vm['name'] if vm else '') + '-' + row['job_id'])
                try:
                    if not vm or vm['state'] != 'READY' or not vm['vmx_path']:
                        raise CleanupError('Current VM is not in READY state')
                    if path != expected or not path.exists():
                        raise CleanupError('Backup path does not match recorded reinstall directory')
                    current_vmx = Path(vm['vmx_path'])
                    if current_vmx != self.vms_root / vm['name'] / (vm['name'] + '.vmx') or not current_vmx.is_file():
                        raise CleanupError('Current replacement VM is missing or has an unexpected path')
                    old_vmx = path / (vm['name'] + '.vmx')
                    if not old_vmx.is_file() or _is_reparse_point(old_vmx):
                        raise CleanupError('Old VMX is absent or unsafe')
                    total, files = _guard_tree(path, backup_root)
                    assert_powered_off(old_vmx, self.vmrun, strict_locks=True)
                    if find_linked_disk_references(self.vms_root, path):
                        raise CleanupError('Another VM disk descriptor may reference this backup')
                    items.append(_entry('reinstall:' + row['job_id'], 'reinstall_backup',
                                        '已完成重装 · ' + vm['name'], path,
                                        total, files, path.stat().st_mtime, True))
                except Exception as exc:
                    protected.append({'path': str(path), 'reason': str(exc)[:180]})
        return {'items': items, 'protected': protected[:50],
                'total_bytes': sum(x['bytes'] for x in items),
                'reinstall_backups': sum(x['kind'] == 'reinstall_backup' for x in items)}

    def delete(self, requested: list[dict], *, confirm_backups: bool = False) -> dict:
        if not requested or len(requested) > 50:
            raise CleanupError('Choose 1 to 50 explicitly listed items')
        ids = [str(r.get('id', '')) for r in requested]
        if len(set(ids)) != len(ids):
            raise CleanupError('Duplicate cleanup selection')
        # Rescan and compare exact paths, sizes, file counts, and mtimes.
        self._check_idle()
        catalog = {row['id']: row for row in self.scan()['items']}
        targets = []
        for obj in requested:
            key = str(obj.get('id', ''))
            item = catalog.get(key)
            if not item or item['fingerprint'] != obj.get('fingerprint'):
                raise CleanupError('Cleanup preview is stale; rescan before deleting')
            if item['kind'] == 'reinstall_backup' and not confirm_backups:
                raise CleanupError('Old VM backups require separate explicit confirmation')
            targets.append(item)
        done = []
        for item in targets:
            # Verify the original candidate is still valid immediately before deletion.
            live = {x['id']: x for x in self.scan()['items']}.get(item['id'])
            if not live or live['fingerprint'] != item['fingerprint']:
                raise CleanupError('File changed during cleanup; stopped')
            path = Path(item['path'])
            if item['kind'] == 'old_log':
                if path.parent != self.root / 'data' / 'updates' or _is_reparse_point(path):
                    raise CleanupError('Unsafe log path')
                path.unlink()
            else:
                parent = path.parent
                _guard_tree(path, parent)
                quarantine = parent / ('.vmo-cleanup-' + os.urandom(12).hex())
                os.replace(path, quarantine)
                try:
                    shutil.rmtree(quarantine)
                except Exception as exc:
                    raise CleanupError('Partial deletion in ' + str(quarantine) +
                                       '; inspect manually; do not rerun') from exc
                if item['kind'] == 'reinstall_backup':
                    self.fleet.mark_reinstall_backup_deleted(item['id'].split(':', 1)[1],
                                                             str(path))
            done.append({'id': item['id'], 'bytes': item['bytes']})
        return {'deleted': done, 'deleted_bytes': sum(x['bytes'] for x in done)}
