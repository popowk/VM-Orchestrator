"""Opt-in verified Release update for portable Windows applications.

Repository origins may be public GitHub, a public HTTPS repository mirror, or
a full-URL GitHub proxy. Network transport and redirect controls are isolated
in release_source_network. Release ZIPs still pass all strict archive,
version, signature/evidence, launcher and single-active-package checks.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

from .portable_catalog import VERSION, inspect_zip, PortableCatalogError
from .release_source_network import (
    ReleaseNetworkError as GitHubSourceError,
    canonical_repo,parse_source,_https_url,fetch_release,download_asset,
)

_MAX_ZIP = 512 * 1024 * 1024

def safe_asset_name(value: str) -> str:
    value=value.strip()
    example=value.replace('{version}','1.2.3').replace('{tag}','v1.2.3')
    if value and (len(value)>180 or
                  not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*\.zip',example,re.I)
                  or '..' in example):
        raise GitHubSourceError('ZIP 文件名只支持安全文件名和 {version}/{tag} 模板变量，不允许目录或通配符')
    return value


def _version_tuple(version: str):
    m=re.fullmatch(r'(?:v)?(\d+)\.(\d+)\.(\d+)(?:[-+][a-zA-Z0-9.-]+)?',version)
    return tuple(map(int,m.groups())) if m else None


class GitHubReleaseUpdater:
    def __init__(self, catalog, fleet, *, get_release=None, download=None):
        self.catalog=catalog
        self.fleet=fleet
        self._get_release=get_release or fetch_release
        self._download=download or download_asset
        # Each portable application has its own serialized Release lane.
        # A 40+ MB download for one software must not block another profile's
        # manual check. The guard only protects lock creation, never network I/O.
        self._profile_guard=threading.Lock()
        self._profile_locks: dict[str, threading.Lock]={}

    def _profile_lock(self, profile_id: str) -> threading.Lock:
        with self._profile_guard:
            return self._profile_locks.setdefault(profile_id, threading.Lock())

    def is_checking(self, profile_id: str) -> bool:
        """Read-only in-process state for UI; does not touch stored result/tag."""
        with self._profile_guard:
            lane=self._profile_locks.get(profile_id)
            return bool(lane and lane.locked())

    def check(self, profile_id: str, *, force: bool=False) -> dict:
        lock=self._profile_lock(profile_id)
        # Manual requests briefly wait for the same profile's in-flight
        # background check. Do not hold an HTTP request indefinitely while a
        # download / ZIP check is slow or a reverse proxy has a short timeout.
        acquired=lock.acquire(timeout=8) if force else lock.acquire(blocking=False)
        if not acquired:
            # Important: a busy attempt is not a FAILED version check, and
            # must not replace the last successful tag / result in SQLite.
            return {'state':'checking','profile_id':profile_id,
                    'message':'此软件 Release 正在后台检查或校验；未重复下载，请稍后刷新检查结果'}
        try:
            return self._check(profile_id,force=force)
        finally:
            lock.release()

    def _check(self, profile_id: str, *, force: bool=False) -> dict:
        p=self.catalog.get(profile_id)
        if not p:
            raise GitHubSourceError('软件档案不存在')
        if not p.get('github_repo'):
            raise GitHubSourceError('请先保存公开 HTTPS 仓库或镜像地址')
        if not force and not p.get('github_enabled'):
            return {'state':'disabled','profile_id':profile_id}
        tag=''
        try:
            source=canonical_repo(p['github_repo'])
            data=self._get_release(source)
            if not isinstance(data,dict) or data.get('draft') or data.get('prerelease'):
                raise GitHubSourceError('未找到可使用的 GitHub 正式 Release')
            tag=str(data.get('tag_name') or '')
            version=tag[1:] if tag.lower().startswith('v') else tag
            if not VERSION.fullmatch(version):
                raise GitHubSourceError('GitHub 正式版本号不符合 VMO 软件版本格式')
            assets=data.get('assets') or []
            if p.get('github_asset'):
                expected_asset=p['github_asset'].replace('{version}',version).replace('{tag}',tag)
                matches=[a for a in assets if a.get('name')==expected_asset]
            else:
                matches=[a for a in assets if str(a.get('name','')).lower().endswith('.zip')]
            if len(matches)!=1:
                raise GitHubSourceError('Release ZIP 文件不唯一或不存在，请填写精确 ZIP 文件名')
            asset=matches[0]
            name=safe_asset_name(str(asset.get('name') or ''))
            if not name or name.lower()!=str(asset['name']).lower():
                raise GitHubSourceError('Release 文件名不是合法 ZIP')
            expected_size=int(asset.get('size') or 0)
            if not (50<=expected_size<=_MAX_ZIP):
                raise GitHubSourceError('GitHub Release ZIP 大小无效或超过 512 MB')
            # Never download an arbitrary URL returned by a compromised API;
            # only a release asset under the configured public repository.
            url=parse_source(source).asset_url(
                tag,name,str(asset.get('browser_download_url') or ''))
            _https_url(url,allowed_hosts={parse_source(source).host})
            active=self.fleet.package(p['active_package_id']) if p.get('active_package_id') else None
            if active:
                previous=_version_tuple(active['version'])
                incoming=_version_tuple(version)
                if previous is not None and incoming is not None and incoming<previous:
                    raise GitHubSourceError('远端 Release 版本低于当前版本，拒绝自动降级')
                if p.get('github_last_tag')==tag and active['version']==version:
                    self.catalog.github_result(profile_id,result='已是当前正式版本，无需重复下载',tag=tag)
                    return {'state':'unchanged','tag':tag,'version':version}
            root=self.fleet.path.parent/'approved_packages'
            root.mkdir(parents=True,exist_ok=True)
            # Keep staged bytes separate from served approved ZIP assets.
            fd,partial=tempfile.mkstemp(dir=str(root),prefix='github-release-',suffix='.part')
            os.close(fd)
            staged=Path(partial)
            staged.unlink()
            try:
                digest,length=self._download(url,staged)
                if length!=expected_size:
                    raise GitHubSourceError('ZIP 实际长度与 GitHub Release 文件元数据不符')
                published_digest=str(asset.get('digest') or '')
                if published_digest.startswith('sha256:') and published_digest[7:].lower()!=digest:
                    raise GitHubSourceError('ZIP SHA-256 与 GitHub 发布校验值不匹配')
                if not zipfile.is_zipfile(staged):
                    raise GitHubSourceError('下载结果不是 ZIP 压缩包')
                inspected=inspect_zip(staged,p['launcher'],display_name=p['display_name'],
                                      version_file=p['version_file'],sha256=digest)
                if inspected['id']!=p['id'] or inspected['folder']!=p['folder']:
                    raise GitHubSourceError('ZIP 顶层目录与已登记软件不同，拒绝覆盖')
                with zipfile.ZipFile(staged) as z:
                    if z.testzip() is not None:
                        raise GitHubSourceError('ZIP CRC 完整性检查失败')
                # A ZIP's own VERSION is the strongest evidence; a GitHub
                # tag is not an independently verified installed version.
                if inspected['version_source'].startswith('zip:'):
                    if inspected['version']!=version:
                        raise GitHubSourceError('ZIP 内部版本和 GitHub Release 标签不一致')
                else:
                    inspected['version']=version
                    inspected['version_source']='github_release_tag'
                    inspected['version_override']=''
                if active and active['sha256'].lower()==digest:
                    self.catalog.github_result(profile_id,result='ZIP 内容与当前生效版本相同',tag=tag)
                    return {'state':'unchanged','tag':tag,'version':version}
                if active and active['version']==version:
                    raise GitHubSourceError('相同版本号但 ZIP 哈希已改变，拒绝静默覆盖')
                # Reject source changes while download is pending: an operator
                # may have disabled/reconfigured the policy in the meantime.
                current=self.catalog.get(profile_id)
                if (not current or current.get('github_repo')!=p['github_repo'] or
                    current.get('github_asset')!=p.get('github_asset') or
                    bool(current.get('github_enabled'))!=bool(p.get('github_enabled')) or
                    current.get('active_package_id')!=p.get('active_package_id')):
                    raise GitHubSourceError('软件版本或更新源配置已变化，丢弃本次下载')
                package_id=str(uuid.uuid4())
                approved=root/(package_id+'.zip')
                os.replace(staged,approved)
                try:
                    self.catalog.upsert(inspected)
                    self.fleet.add_package('portable-'+p['id'],version,
                                           digest,length,name,package_id)
                    self.catalog.record_package(package_id,inspected)
                except Exception:
                    # A failed DB record must not turn into an active deployment.
                    # Leave orphaned DB metadata for audit if rollback fails.
                    if self.catalog.get(profile_id).get('active_package_id')!=package_id:
                        approved.unlink(missing_ok=True)
                    raise
                auto=self.fleet.reconcile_auto_deploy() if current.get('auto_deploy') else {
                    'queued':[],'skipped':[]}
                self.catalog.github_result(profile_id,result='发布通过，已激活 '+version,tag=tag)
                return {'state':'published','tag':tag,'version':version,
                        'sha256':digest,'package_id':package_id,
                        'auto_queued':len(auto['queued']),
                        'auto_skipped':len(auto['skipped'])}
            finally:
                staged.unlink(missing_ok=True)
        except Exception as exc:
            # Never leak full signed download links or exception tracebacks.
            safe=str(exc) if isinstance(exc,GitHubSourceError) else (
                'Release 校验或发布失败（'+type(exc).__name__+'）')
            self.catalog.github_result(profile_id,error=safe,tag=tag)
            raise GitHubSourceError(safe) from exc

    def check_enabled(self) -> dict:
        checked=published=0
        for profile in self.catalog.all():
            if not profile.get('github_enabled'):
                continue
            try:
                result=self.check(profile['id'])
                if result['state']=='checking':
                    continue
                checked+=1
                published+=int(result['state']=='published')
            except GitHubSourceError:
                checked+=1
        return {'checked':checked,'published':published}
