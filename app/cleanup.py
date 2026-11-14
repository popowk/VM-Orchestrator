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




def _guard_file(path: Path, managed_root: Path) -> os.stat_result:
    """Reject links and escaped paths, including junctions on ancestors."""
    if not path.is_file() or _is_reparse_point(path):
        raise CleanupError('Not a regular managed file')
    ancestor = path.parent
    while ancestor != managed_root:
        if _is_reparse_point(ancestor) or ancestor == ancestor.parent:
            raise CleanupError('Path uses an unsafe ancestor')
        ancestor = ancestor.parent
    if _is_reparse_point(managed_root) or not path.resolve().is_relative_to(managed_root.resolve()):
        raise CleanupError('File escaped the managed root')
    return path.stat()


def _file_entry(item_id: str, kind: str, title: str, path: Path,
                root: Path, manual: bool, reason: str) -> dict:
    st = _guard_file(path, root)
    item = _entry(item_id, kind, title, path, st.st_size, 1, st.st_mtime, manual)
    item['reason'] = reason
    return item


def _release_manifest(root: Path, protected: list) -> dict | None:
    manifest = root / 'release-manifest.json'
    try:
        data = json.loads(manifest.read_text(encoding='utf-8-sig'))
        installed = (root / 'VERSION').read_text(encoding='utf-8-sig').strip()
        if data.get('version') != installed or not isinstance(data.get('files'), dict):
            raise ValueError('Release manifest version does not match installed version')
        return data['files']
    except (OSError, ValueError, TypeError) as exc:
        protected.append({'path': str(manifest),
                          'reason': '当前版本文件清单不存在或不匹配；无法判断程序目录内额外文件：' + str(exc)[:100]})
        return None

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
        items, protected, missing_records = [], [], []
        now = time.time()
        updates = self.root / 'data' / 'updates'
        current = (self.root / 'VERSION').read_text(encoding='utf-8-sig').strip() if (self.root / 'VERSION').exists() else ''
        if updates.is_dir() and not _is_reparse_point(updates):
            status_file = updates / 'status.json'
            try:
                status = json.loads(status_file.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                status = {}
            attempt = str(status.get('attempt') or '')
            # An idle/successful updater needs none of these stage directories.
            # The old seven-day cutoff concealed newly superseded releases.
            for candidate in sorted(updates.iterdir()):
                if not re.fullmatch(r'(?:stage|unpacked)-v\d+\.\d+\.\d+', candidate.name):
                    continue
                try:
                    total, count = _guard_tree(candidate, updates)
                    item = _entry('stage:' + candidate.name, 'program_cache',
                                  '更新解压缓存 · ' + candidate.name, candidate,
                                  total, count, candidate.stat().st_mtime, False)
                    item['reason'] = '更新流程结束后不再运行此目录；删除前重新校验更新状态'
                    items.append(item)
                except (OSError, CleanupError) as exc:
                    protected.append({'path': str(candidate), 'reason': '无法安全清理：' + str(exc)})
            # The current launcher copy and latest logs may still be needed for
            # diagnosing a recent installation. Keep them even after success.
            keep_names = {'status.json', 'dependencies.log', 'host-launch-out.log',
                          'host-launch-error.log', 'Repair-Updater.ps1'}
            keep_names.update({'Apply-GitHubUpdate-v' + current + '.ps1'})
            if re.fullmatch(r'[0-9a-fA-F]{32}', attempt):
                keep_names.update({'updater-' + attempt + '.log',
                                   'updater-bootstrap-' + attempt + '.log',
                                   'updater-ack-' + attempt + '.txt'})
            for candidate in sorted(updates.iterdir()):
                if candidate.is_dir():
                    continue
                name = candidate.name
                if _is_reparse_point(candidate):
                    protected.append({'path': str(candidate), 'reason': '链接或重解析文件不能自动删除'})
                    continue
                if not candidate.is_file():
                    continue
                if name in keep_names:
                    protected.append({'path': str(candidate), 'reason': '保留当前更新状态、排错记录或人工维修脚本'})
                    continue
                generated = (
                    re.fullmatch(r'Apply-GitHubUpdate-v\d+\.\d+\.\d+\.ps1', name)
                    or re.fullmatch(r'Apply-GitHubUpdate\.backup-[\w.-]+\.ps1', name)
                    or re.fullmatch(r'updater-[0-9a-fA-F]{32}\.log', name)
                    or re.fullmatch(r'updater-bootstrap-[0-9a-fA-F]{32}\.log', name)
                    or re.fullmatch(r'updater-ack-[0-9a-fA-F]{32}\.txt', name)
                    or re.fullmatch(r'(?:status|download)-[0-9a-fA-F]{32}\.tmp', name)
                )
                if not generated:
                    protected.append({'path': str(candidate), 'reason': '未知 data/updates 文件；未核实用途，不自动删除'})
                    continue
                try:
                    manual = name.startswith('Apply-GitHubUpdate.backup-') or name.endswith('.log')
                    items.append(_file_entry('artifact:' + name, 'update_artifact',
                                            '旧更新附属文件 · ' + name, candidate,
                                            self.root, bool(manual),
                                            '非当前更新尝试生成的文件；日志/脚本备份需手动勾选'))
                except (OSError, CleanupError) as exc:
                    protected.append({'path': str(candidate), 'reason': str(exc)})
            backup_root = updates / 'backups'
            if backup_root.is_dir() and not _is_reparse_point(backup_root):
                names = sorted((x for x in backup_root.iterdir()
                                if re.fullmatch(r'(?:before|hot)-v\d+\.\d+\.\d+-[A-Za-z0-9-]+', x.name)),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                # The newest two code backups are unconditional rollback protection.
                for candidate in names[:2]:
                    protected.append({'path': str(candidate), 'reason': '最近两份代码回滚备份，强制保留'})
                for candidate in names[2:]:
                    try:
                        total, count = _guard_tree(candidate, backup_root)
                        item = _entry('code:' + candidate.name, 'code_backup',
                                      '历史代码回滚备份 · ' + candidate.name, candidate,
                                      total, count, candidate.stat().st_mtime, True)
                        item['reason'] = '存在可用性依赖风险；需要人工确认不再用于回滚'
                        items.append(item)
                    except (OSError, CleanupError) as exc:
                        protected.append({'path': str(candidate), 'reason': str(exc)})
        # Compare managed program trees with this version's build-time manifest.
        # Never infer that unrecognized software is disposable without review.
        manifest = _release_manifest(self.root, protected)
        if manifest is not None:
            allowed_suffixes = {'.py', '.ps1', '.cmd', '.bat', '.php', '.js', '.css', '.html', '.json'}
            for folder in ('app', 'guest_agent', 'scripts', 'integrations'):
                base = self.root / folder
                if not base.is_dir() or _is_reparse_point(base):
                    continue
                for directory, dirs, files in os.walk(base, followlinks=False):
                    parent = Path(directory)
                    safe_dirs = []
                    for name in dirs:
                        sub = parent / name
                        if _is_reparse_point(sub):
                            protected.append({'path': str(sub), 'reason': '自定义目录或链接；不进入扫描'})
                        elif name not in ('__pycache__', '.git', 'node_modules'):
                            safe_dirs.append(name)
                    dirs[:] = safe_dirs
                    for name in files:
                        candidate = parent / name
                        rel = candidate.relative_to(self.root).as_posix()
                        if rel in manifest:
                            # A modified or locally patched source must be protected.
                            expected = manifest[rel]
                            try:
                                if candidate.stat().st_size != expected.get('size') or (
                                    candidate.stat().st_size <= 4 * 1024 * 1024 and
                                    hashlib.sha256(candidate.read_bytes()).hexdigest() != expected.get('sha256')):
                                    protected.append({'path': str(candidate), 'reason': '与当前发行版校验值不同；保留本地修改'})
                            except (OSError, ValueError, TypeError):
                                protected.append({'path': str(candidate), 'reason': '程序文件校验失败，保留'})
                            continue
                        if candidate.suffix.lower() not in allowed_suffixes:
                            continue
                        try:
                            items.append(_file_entry('extra:' + rel, 'program_extra',
                                                     '非当前版本程序文件 · ' + rel,
                                                     candidate, self.root, True,
                                                     '不在当前版本清单中；可能是自定义脚本/插件，必须人工核对'))
                        except (OSError, CleanupError) as exc:
                            protected.append({'path': str(candidate), 'reason': str(exc)})
        # Only recognize known temporary file suffixes directly under data.
        # Database, certificates, sessions, keys and subdirectories are never candidates.
        data_root = self.root / 'data'
        if data_root.is_dir() and not _is_reparse_point(data_root):
            for candidate in data_root.iterdir():
                if not candidate.is_file() or _is_reparse_point(candidate):
                    continue
                if not re.fullmatch(r'(?:status|download|upgrade)-[0-9a-fA-F]{32}\.tmp', candidate.name):
                    continue
                try:
                    if now - candidate.stat().st_mtime >= 86400:
                        items.append(_file_entry('data:' + candidate.name, 'data_cache',
                                                 '历史状态临时文件 · ' + candidate.name,
                                                 candidate, self.root, True,
                                                 '运行数据目录下的过期临时文件，需手动确认'))
                except (OSError, CleanupError):
                    pass
        backup_root = self.vms_root / '.vmo-reinstall-backups'
        if backup_root.is_dir() and not _is_reparse_point(backup_root):
            with self.fleet.db() as d:
                rows = [dict(row) for row in d.execute(
                    "SELECT job_id,vm_id,backup_path,status,backup_deleted_at "
                    "FROM vm_reinstalls WHERE status='SUCCEEDED' AND "
                    "backup_deleted_at IS NULL AND backup_missing_at IS NULL AND backup_path IS NOT NULL ORDER BY completed_at DESC")]
            for row in rows[:1000]:
                path = Path(row['backup_path'])
                vm = self.store.get_vm(row['vm_id'])
                expected = backup_root / ((vm['name'] if vm else '') + '-' + row['job_id'])
                try:
                    if not vm or vm['state'] != 'READY' or not vm['vmx_path']:
                        raise CleanupError('Current VM is not in READY state')
                    if path != expected:
                        raise CleanupError('备份路径与当前虚拟机重装记录不匹配，禁止自动处理')
                    if not path.exists():
                        # The audit row is real but the filesystem copy is gone.
                        # Preview a database-only acknowledgement, never a
                        # deletable folder. Preserve audit history.
                        missing_records.append({
                            'id': 'missing:' + row['job_id'],
                            'vm_id': row['vm_id'], 'job_id': row['job_id'],
                            'path': str(path),
                            'reason': '重装备份文件缺失；只可标记数据库状态，不会释放磁盘空间',
                            'bytes': 0
                        })
                        continue
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
            # Unknown folders under the dedicated backup root are never
            # deletable merely because their names look like backup IDs.
            # Report as protected for manual audit, not cleanup candidates.
            known={str(Path(row['backup_path'])) for row in rows}
            try:
                for candidate in list(backup_root.iterdir())[:300]:
                    if str(candidate) not in known:
                        protected.append({'path': str(candidate),
                                          'reason': '重装备份目录未关联当前成功重装记录，禁止直接删除'})
            except OSError as exc:
                protected.append({'path': str(backup_root),'reason': str(exc)[:150]})
        return {'items': items, 'protected': protected[:300],
                'protected_count': len(protected),
                'missing_records': missing_records,
                'missing_count': len(missing_records),
                'total_bytes': sum(x['bytes'] for x in items),
                'reinstall_backups': sum(x['kind'] == 'reinstall_backup' for x in items)}

    def acknowledge_missing(self, job_ids: list[str]) -> dict:
        """Mark missing backup records for audit; NEVER delete files or audit rows."""
        if not job_ids or len(job_ids)>50 or len(set(job_ids))!=len(job_ids):
            raise CleanupError('请选择 1–50 条不重复的失效备份记录')
        self._check_idle()
        scan=self.scan()
        records={x['job_id']:x for x in scan['missing_records']}
        if any(job not in records for job in job_ids):
            raise CleanupError('待确认的失效记录已变化，请重新扫描')
        # Double-check all paths before changing any database state.
        for job in job_ids:
            item=records[job]
            path=Path(item['path'])
            if path.exists() or path.is_symlink():
                raise CleanupError('备份目录重新出现；停止同步数据库状态')
            vm=self.store.get_vm(item['vm_id'])
            expected=self.vms_root / '.vmo-reinstall-backups' / (
                (vm['name'] if vm else '')+'-'+job)
            if not vm or path!=expected:
                raise CleanupError('备份路径或虚拟机状态发生变化，拒绝修改')
        for job in job_ids:
            item=records[job]
            self.fleet.mark_reinstall_backup_missing(job,item['path'])
        return {'acknowledged':job_ids,'files_deleted':0,'bytes_released':0}

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
            if item['kind'] in ('old_log', 'update_artifact', 'program_extra', 'data_cache'):
                _guard_file(path, self.root)
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
