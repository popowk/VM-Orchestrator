"""Server-side, opt-in GitHub Release updater for reviewed portable C:\dsh apps.

The only external origin is a public github.com/owner/repo RELEASE, never a
GitHub source-code archive or a user-supplied binary URL. Release ZIPs go
through the same strict inspect_zip, CRC, SHA256, launcher, program folder
and active-version checks as admin uploaded software. The auto-push policy
remains separate and disabled unless already enabled for this application.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

from .portable_catalog import VERSION, inspect_zip, PortableCatalogError

_OWNER = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z')
_REPO = re.compile(r'[A-Za-z0-9_.-]{1,100}\Z')
_ALLOWED_HOSTS = frozenset({
    'api.github.com', 'github.com',
    'release-assets.githubusercontent.com',
    'objects.githubusercontent.com',
})
_MAX_ZIP = 512 * 1024 * 1024
_MAX_JSON = 2 * 1024 * 1024


class GitHubSourceError(ValueError):
    pass


def canonical_repo(url: str) -> str:
    if not isinstance(url, str) or len(url)>220:
        raise GitHubSourceError('GitHub 仓库地址无效')
    u=urllib.parse.urlsplit(url.strip())
    if (u.scheme!='https' or u.netloc.lower()!='github.com' or
            u.username is not None or u.password is not None or
            u.port is not None or u.query or u.fragment):
        raise GitHubSourceError('只允许 https://github.com/所有者/仓库 的公开仓库地址')
    parts=u.path.strip('/').split('/')
    if len(parts)!=2 or not _OWNER.fullmatch(parts[0]) or not _REPO.fullmatch(parts[1]):
        raise GitHubSourceError('GitHub 地址仅填写仓库根地址，不要填 Release 或 ZIP 的下载链接')
    if parts[1].lower().endswith('.git'):
        raise GitHubSourceError('请填写 GitHub 网页仓库地址，不要填写 .git 克隆地址')
    return f'https://github.com/{parts[0]}/{parts[1]}'


def safe_asset_name(value: str) -> str:
    value=value.strip()
    if value and (len(value)>180 or
                  not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*\.zip',value,re.I)
                  or '..' in value):
        raise GitHubSourceError('ZIP 文件名必须是 Release 内的单个 ZIP 文件名，不能含目录、通配符或参数')
    return value


def _https_url(url: str, *, api: bool=False) -> str:
    u=urllib.parse.urlsplit(url)
    host=(u.hostname or '').lower()
    if (u.scheme!='https' or host not in _ALLOWED_HOSTS or
            u.username is not None or u.password is not None or
            u.port not in (None,443)):
        raise GitHubSourceError('GitHub Release 出现了不安全的下载重定向，已停止')
    if api and host!='api.github.com':
        raise GitHubSourceError('只可通过 GitHub 官方 API 检查 Release')
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


_OPENER=urllib.request.build_opener(_SafeRedirect())


def fetch_release(repo_url: str) -> dict:
    path=urllib.parse.urlsplit(canonical_repo(repo_url)).path.strip('/')
    url=_https_url('https://api.github.com/repos/'+path+'/releases/latest',api=True)
    req=urllib.request.Request(url,headers={
        'Accept':'application/vnd.github+json',
        'User-Agent':'VM-Orchestrator-Portable-Release-Updater',
        'X-GitHub-Api-Version':'2022-11-28'})
    try:
        with _OPENER.open(req,timeout=18) as response:
            if int(response.headers.get('Content-Length') or 0)>_MAX_JSON:
                raise GitHubSourceError('Release 元数据过大')
            blob=response.read(_MAX_JSON+1)
            if len(blob)>_MAX_JSON:
                raise GitHubSourceError('Release 元数据超过限制')
            return json.loads(blob.decode('utf-8'))
    except (OSError,ValueError) as exc:
        raise GitHubSourceError('无法读取公开 GitHub 最新正式 Release（网络/限流/仓库权限）') from exc


def download_asset(url: str, destination: Path) -> tuple[str,int]:
    """Stream a trusted GitHub release asset; cap bytes on every transfer."""
    _https_url(url)
    request=urllib.request.Request(url,headers={
        'User-Agent':'VM-Orchestrator-Portable-Release-Updater',
        'Accept':'application/octet-stream'})
    hasher=hashlib.sha256()
    size=0
    try:
        with _OPENER.open(request,timeout=65) as response:
            if int(response.headers.get('Content-Length') or 0)>_MAX_ZIP:
                raise GitHubSourceError('Release ZIP 超过 512 MB')
            with destination.open('xb') as out:
                while True:
                    chunk=response.read(1024*1024)
                    if not chunk:break
                    size+=len(chunk)
                    if size>_MAX_ZIP:
                        raise GitHubSourceError('Release ZIP 超过 512 MB')
                    out.write(chunk)
                    hasher.update(chunk)
    except OSError as exc:
        raise GitHubSourceError('GitHub Release ZIP 下载失败') from exc
    if size<50:
        raise GitHubSourceError('GitHub Release ZIP 文件太小，可能是错误页面')
    return hasher.hexdigest(),size


def _version_tuple(version: str):
    m=re.fullmatch(r'(?:v)?(\d+)\.(\d+)\.(\d+)(?:[-+][a-zA-Z0-9.-]+)?',version)
    return tuple(map(int,m.groups())) if m else None


class GitHubReleaseUpdater:
    def __init__(self, catalog, fleet, *, get_release=None, download=None):
        self.catalog=catalog
        self.fleet=fleet
        self._get_release=get_release or fetch_release
        self._download=download or download_asset
        self._lock=threading.Lock()

    def check(self, profile_id: str, *, force: bool=False) -> dict:
        if not self._lock.acquire(blocking=False):
            raise GitHubSourceError('已有软件 Release 检查正在运行，请稍后重试')
        try:
            return self._check(profile_id,force=force)
        finally:
            self._lock.release()

    def _check(self, profile_id: str, *, force: bool=False) -> dict:
        p=self.catalog.get(profile_id)
        if not p:
            raise GitHubSourceError('软件档案不存在')
        if not p.get('github_repo'):
            raise GitHubSourceError('请先保存 GitHub 公开仓库地址')
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
                matches=[a for a in assets if a.get('name')==p['github_asset']]
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
            url=str(asset.get('browser_download_url') or '')
            prefix=source+'/releases/download/'
            if not url.startswith(prefix):
                raise GitHubSourceError('Release 下载地址不属于已绑定仓库')
            _https_url(url)
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
                    (not force and not current.get('github_enabled'))):
                    raise GitHubSourceError('更新源配置已更改，丢弃本次下载')
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
                checked+=1
                published+=int(result['state']=='published')
            except GitHubSourceError:
                checked+=1
        return {'checked':checked,'published':published}
