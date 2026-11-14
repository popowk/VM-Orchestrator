"""Reviewed ZIP catalog for portable applications installed under Guest C:\dsh.

The uploaded archive must contain exactly ONE top-level program folder. The
directory name in the ZIP is the actual install folder, never an arbitrary
path supplied through the browser. Profile definitions are stored in SQLite
and bound to an immutable, SHA-256 checked update package.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

VERSION = re.compile(r'^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$')
# Relative files and programs only, never a command, argument list, URL, or path.
BAD_NAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
RESERVED = re.compile(r'(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$')
MAX_ENTRIES = 20000
MAX_UNPACKED = 2 * 1024 ** 3
PRESERVE_NAMES = {'data', 'config', 'profiles', 'credentials', 'logs', 'soul.db',
                  'fish-soul-config.json', 'sessions', '.env'}


class PortableCatalogError(ValueError):
    pass


def relative_file(name: str) -> str:
    if not isinstance(name, str) or not (1 <= len(name) <= 100):
        raise PortableCatalogError('启动文件或版本文件名称无效')
    parts = name.replace('\\', '/').split('/')
    if len(parts) > 5 or any(not p or p in ('.', '..') or BAD_NAME.search(p)
                           or RESERVED.fullmatch(p) or p.endswith((' ', '.')) for p in parts):
        raise PortableCatalogError('只允许程序目录内的相对文件名，不能包含路径穿越或命令参数')
    return '/'.join(parts)


def folder_name(folder: str) -> str:
    if not isinstance(folder, str) or not (1 <= len(folder) <= 75) or BAD_NAME.search(folder):
        raise PortableCatalogError('压缩包顶层程序文件夹名称不合法')
    if folder in ('.', '..') or folder.endswith((' ', '.')) or RESERVED.fullmatch(folder) or folder[0] == ' ':
        raise PortableCatalogError('压缩包顶层文件夹名称不合法')
    if folder.casefold().startswith(('.vmo-', 'guestagent')) or folder.casefold() in {'windows','users','system32','program files','programdata'}:
        raise PortableCatalogError('不允许覆盖管理工具或 Windows 系统目录')
    return folder


def profile_id_for(folder: str) -> str:
    return 'p-' + hashlib.sha256(folder.casefold().encode('utf-8')).hexdigest()[:20]


def _hash_archive(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def _read_small(archive: zipfile.ZipFile, name: str, size: int = 32768) -> str:
    member = archive.getinfo(name)
    if member.file_size > size:
        raise PortableCatalogError('版本描述文件超过允许长度')
    return archive.read(name).decode('utf-8-sig', 'replace').strip()


def _extract_version(archive: zipfile.ZipFile, folder: str, custom_file: str) -> tuple[str, str]:
    files = {i.filename.replace('\\', '/') for i in archive.infolist() if not i.is_dir()}
    candidates = [custom_file] if custom_file else []
    candidates += ['VERSION', 'version.txt', 'version', 'package.json', 'app.json', 'manifest.json']
    for relative in dict.fromkeys(x for x in candidates if x):
        member = folder + '/' + relative
        if member not in files:
            continue
        try:
            content = _read_small(archive, member)
            if relative.lower().endswith('.json'):
                payload = json.loads(content)
                value = payload.get('version') or payload.get('appVersion') if isinstance(payload, dict) else ''
            else:
                value = content.splitlines()[0].strip() if content else ''
            if isinstance(value, str) and VERSION.fullmatch(value):
                return value, relative
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    return '', ''


def inspect_zip(archive_file: Path, launcher: str, *, display_name: str,
                version_override: str = '', version_file: str = '', sha256: str = '') -> dict:
    if not isinstance(display_name, str) or not (1 <= len(display_name.strip()) <= 70):
        raise PortableCatalogError('请填写软件名称（不超过70个字符）')
    launcher = relative_file(launcher)
    version_file = relative_file(version_file) if version_file else ''
    if version_override and not VERSION.fullmatch(version_override):
        raise PortableCatalogError('手动版本号格式无效')
    try:
        with zipfile.ZipFile(archive_file) as z:
            members = z.infolist()
            files = [x for x in members if not x.is_dir()]
            if not files or len(members) > MAX_ENTRIES or sum(x.file_size for x in files) > MAX_UNPACKED:
                raise PortableCatalogError('压缩包为空或超出最大文件数量/解压限制')
            roots = set()
            seen = set()
            for entry in members:
                item = entry.filename.replace('\\','/').rstrip('/')
                components = item.split('/')
                if len(components) < 2 and not entry.is_dir():
                    raise PortableCatalogError('ZIP 只能包含一个顶层程序文件夹，不允许根目录散放文件')
                for piece in components:
                    folder_name(piece) if piece == components[0] else relative_file(piece)
                if (entry.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
                    raise PortableCatalogError('不允许 ZIP 含符号链接')
                if (entry.external_attr >> 16) & 0o170000 not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise PortableCatalogError('ZIP 不允许特殊设备文件')
                normalized = '/'.join(components).casefold()
                if normalized in seen:
                    raise PortableCatalogError('ZIP 包含重复或大小写冲突路径')
                seen.add(normalized)
                roots.add(components[0])
            if len(roots) != 1:
                raise PortableCatalogError('ZIP 内只能有一个程序目录')
            folder = folder_name(next(iter(roots)))
            paths = {x.filename.replace('\\', '/') for x in files}
            if folder + '/' + launcher not in paths:
                raise PortableCatalogError('ZIP 中没有所指定的启动文件：' + launcher)
            extension = Path(launcher).suffix.lower()
            if extension not in ('.exe', '.bat', '.cmd', '.lnk'):
                raise PortableCatalogError('启动文件仅支持 EXE/BAT/CMD/LNK，且不能附带启动参数')
            if extension == '.lnk':
                raise PortableCatalogError('快捷方式可能指向程序目录外，请使用实际 EXE/BAT/CMD')
            detected, evidence = _extract_version(z, folder, version_file)
            if version_override:
                version, source = version_override, 'manual'
            elif detected:
                version, source = detected, 'zip:' + evidence
            else:
                # An opaque build fingerprint is NOT a verified application version.
                version, source = 'build-' + (sha256 or _hash_archive(archive_file))[:12], 'zip_sha256'
            return {'id': profile_id_for(folder),
                    'display_name': display_name.strip(), 'folder': folder, 'launcher': launcher,
                    'version': version, 'version_source': source,
                    'version_file': version_file,
                    'version_override': version_override}
    except zipfile.BadZipFile as exc:
        raise PortableCatalogError('ZIP 文件损坏，不能建立软件更新包') from exc


class PortableCatalog:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        with self._db() as d:
            d.execute('''CREATE TABLE IF NOT EXISTS portable_profiles (
                id TEXT PRIMARY KEY, display_name TEXT NOT NULL, folder TEXT NOT NULL,
                launcher TEXT NOT NULL, version_file TEXT NOT NULL DEFAULT '',
                version_source TEXT NOT NULL DEFAULT 'auto', version_override TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            d.execute('''CREATE TABLE IF NOT EXISTS portable_package_meta (
                package_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                version_source TEXT NOT NULL, version_file TEXT NOT NULL,
                uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            d.execute('CREATE UNIQUE INDEX IF NOT EXISTS portable_profiles_folder ON portable_profiles(folder COLLATE NOCASE)')
            columns={row[1] for row in d.execute('PRAGMA table_info(portable_profiles)')}
            if 'auto_deploy' not in columns:
                d.execute('ALTER TABLE portable_profiles ADD COLUMN auto_deploy INTEGER NOT NULL DEFAULT 0')
            if 'active_package_id' not in columns:
                d.execute("ALTER TABLE portable_profiles ADD COLUMN active_package_id TEXT NOT NULL DEFAULT ''")
                # One initial active release per existing software; older ZIPs
                # remain archived for task history and explicit deletion.
                if d.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='update_packages'").fetchone():
                    d.execute("""
                        UPDATE portable_profiles SET active_package_id=COALESCE((
                            SELECT id FROM update_packages
                            WHERE product='portable-'||portable_profiles.id AND deleted_at IS NULL
                            ORDER BY uploaded_at DESC, rowid DESC LIMIT 1), '')
                    """)
            d.execute('CREATE INDEX IF NOT EXISTS portable_package_meta_profile ON portable_package_meta(profile_id,uploaded_at)')
            source_columns={
                'github_repo': "TEXT NOT NULL DEFAULT ''",
                'github_enabled': "INTEGER NOT NULL DEFAULT 0",
                'github_asset': "TEXT NOT NULL DEFAULT ''",
                'github_last_checked': 'TEXT',
                'github_last_result': "TEXT NOT NULL DEFAULT ''",
                'github_last_error': "TEXT NOT NULL DEFAULT ''",
                'github_last_tag': "TEXT NOT NULL DEFAULT ''",
            }
            for name,ddl in source_columns.items():
                if name not in columns:
                    d.execute(f'ALTER TABLE portable_profiles ADD COLUMN {name} {ddl}')


    @contextmanager
    def _db(self):
        con = sqlite3.connect(str(self.db_path), timeout=20)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def upsert(self, profile: dict) -> dict:
        with self._db() as d:
            existing = d.execute('SELECT id,folder,launcher FROM portable_profiles WHERE folder=? COLLATE NOCASE',
                                 (profile['folder'],)).fetchone()
            if existing and existing['id'] != profile['id']:
                raise PortableCatalogError('该目录已登记为不同软件，请先核对目录')
            other=d.execute('SELECT id FROM portable_profiles WHERE display_name=? COLLATE NOCASE AND id<>? LIMIT 1',
                            (profile['display_name'],profile['id'])).fetchone()
            if other:
                raise PortableCatalogError('该软件名称已绑定另一程序目录，同一软件不可登记多个目录')
            if existing and existing['folder'] != profile['folder']:
                raise PortableCatalogError('同一软件 ZIP 顶层目录名的大小写必须与已登记目录一致')
            # Never silently switch a registered launch path: explicit review.
            if existing and existing['launcher'].casefold() != profile['launcher'].casefold():
                raise PortableCatalogError('该软件已登记其他启动文件。为避免误启动，请使用原启动文件或另行修改软件配置')
            d.execute('''INSERT INTO portable_profiles
                (id,display_name,folder,launcher,version_file,version_source,version_override)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,
                version_file=excluded.version_file, version_source=excluded.version_source,
                version_override=excluded.version_override,updated_at=CURRENT_TIMESTAMP''',
                (profile['id'], profile['display_name'], profile['folder'],
                 profile['launcher'], profile['version_file'],
                 profile['version_source'], profile['version_override']))
        return profile

    def record_package(self, package_id: str, profile: dict):
        """Publish exactly one active release per folder and retire old queued tasks."""
        with self._db() as d:
            d.execute('BEGIN IMMEDIATE')
            d.execute('''INSERT INTO portable_package_meta
                (package_id,profile_id,version_source,version_file) VALUES(?,?,?,?)''',
                (package_id, profile['id'], profile['version_source'],
                 profile['version_file']))
            result=d.execute('UPDATE portable_profiles SET active_package_id=? WHERE id=?',
                             (package_id,profile['id']))
            if result.rowcount!=1:
                raise PortableCatalogError('软件目录档案不存在，无法激活版本')
            if d.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guest_tasks'").fetchone():
                d.execute('''UPDATE guest_tasks SET status='CANCELLED',
                    message='已由同软件的新生效版本替代，旧任务未执行',completed_at=CURRENT_TIMESTAMP
                    WHERE status='QUEUED' AND package_id!=? AND package_id IN (
                        SELECT id FROM update_packages WHERE product=?)''',
                    (package_id,'portable-'+profile['id']))

    def configure_github(self, profile_id: str, repo_url: str,
                         enabled: bool, asset_name: str) -> dict:
        """GitHub source is separate from the customer auto-push policy."""
        from .github_releases import canonical_repo, safe_asset_name
        url=canonical_repo(repo_url) if repo_url.strip() else ''
        asset=safe_asset_name(asset_name)
        if enabled and not url:
            raise PortableCatalogError('先填写 GitHub 公开仓库网址才能启用自动检查')
        with self._db() as d:
            d.execute('BEGIN IMMEDIATE')
            row=d.execute('SELECT id,github_repo FROM portable_profiles WHERE id=?',
                          (profile_id,)).fetchone()
            if not row:
                raise PortableCatalogError('软件档案不存在，请先上传首次软件 ZIP')
            other=d.execute('SELECT id FROM portable_profiles WHERE github_repo=? AND id<>? LIMIT 1',
                            (url,profile_id)).fetchone() if url else None
            if other:
                raise PortableCatalogError('这个 GitHub 仓库已绑定另一个软件目录')
            changed=url!=row['github_repo']
            d.execute('''UPDATE portable_profiles SET github_repo=?,github_enabled=?,
                         github_asset=?,updated_at=CURRENT_TIMESTAMP,
                         github_last_error='', github_last_result=CASE WHEN ? THEN '' ELSE github_last_result END,
                         github_last_tag=CASE WHEN ? THEN '' ELSE github_last_tag END
                         WHERE id=?''',
                      (url,int(enabled),asset,int(changed),int(changed),profile_id))
        return self.get(profile_id)

    def github_result(self, profile_id: str, *,
                      result: str='', error: str='', tag: str='') -> None:
        # Never store URLs with GitHub signed redirect tokens.
        with self._db() as d:
            d.execute('''UPDATE portable_profiles SET github_last_checked=CURRENT_TIMESTAMP,
                  github_last_result=?,github_last_error=?,github_last_tag=?
                  WHERE id=?''',(result[:200],error[:240],tag[:60],profile_id))

    def set_auto_deploy(self, profile_id: str, enabled: bool) -> dict:
        """Persist the operator's explicit fleet-wide deployment policy."""
        with self._db() as d:
            d.execute('BEGIN IMMEDIATE')
            current=d.execute('SELECT id,active_package_id FROM portable_profiles WHERE id=?',
                              (profile_id,)).fetchone()
            if not current:
                raise PortableCatalogError('软件档案不存在')
            if enabled and not current['active_package_id']:
                raise PortableCatalogError('该软件尚无当前生效的 ZIP 版本')
            d.execute('UPDATE portable_profiles SET auto_deploy=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                      (int(enabled),profile_id))
            if not enabled:
                d.execute('''UPDATE guest_tasks SET status='CANCELLED',
                    message='管理员关闭了此软件的自动推送；任务未执行',completed_at=CURRENT_TIMESTAMP
                    WHERE status='QUEUED' AND package_id IN (
                        SELECT id FROM update_packages WHERE product=?)
                    AND json_extract(options_json,'$.auto_deploy')=1''',
                    ('portable-'+profile_id,))
        return self.get(profile_id)


    def release_metadata(self, package_id: str) -> dict:
        with self._db() as d:
            r=d.execute(
                'SELECT profile_id,version_source,version_file FROM portable_package_meta WHERE package_id=?',
                (package_id,)).fetchone()
            return dict(r) if r else {}

    def get(self, profile_id: str) -> dict | None:
        with self._db() as d:
            r = d.execute('SELECT * FROM portable_profiles WHERE id=?', (profile_id,)).fetchone()
            return dict(r) if r else None

    def all(self) -> list[dict]:
        with self._db() as d:
            return [dict(row) for row in d.execute(
                'SELECT * FROM portable_profiles ORDER BY display_name COLLATE NOCASE')]

    def profile_for_product(self, product: str) -> dict | None:
        return self.get(product[len('portable-'):]) if product.startswith('portable-p-') else None
