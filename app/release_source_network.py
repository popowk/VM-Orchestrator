"""HTTPS-only Release transport for GitHub, public API mirrors and Gitea.

The configured repository may be a direct github.com link, a reverse-proxied
public repository (example https://mirror.example/owner/repo), or a full-URL
GitHub proxy (https://proxy.example/https://github.com/owner/repo).
Only a known releases/latest JSON endpoint and a matching versioned ZIP asset
can be accessed. Public DNS is checked and the socket is pinned to a checked
public address before TLS verification, including every redirect.

System HTTP_PROXY environment variables are deliberately not used: a request
cannot silently bypass the public-address check through an implicit proxy.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_OWNER = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z')
_REPO = re.compile(r'[A-Za-z0-9_.-]{1,100}\Z')
_GITHUB_CDN = frozenset({
    'github.com', 'api.github.com', 'release-assets.githubusercontent.com',
    'objects.githubusercontent.com', 'github-releases.githubusercontent.com',
})
_MAX_JSON = 2 * 1024 * 1024
_MAX_ZIP = 512 * 1024 * 1024
_BANNED_SUFFIXES = ('.localhost', '.local', '.internal', '.test', '.invalid', '.onion')


class ReleaseNetworkError(ValueError):
    pass


def _hostname(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        hostname = (p.hostname or '').lower().rstrip('.')
        port = p.port
    except ValueError as exc:
        raise ReleaseNetworkError('更新源 URL 格式错误') from exc
    if (p.scheme != 'https' or not hostname or port not in (None, 443) or
            p.username is not None or p.password is not None or
            p.fragment or
            hostname == 'localhost' or hostname.endswith(_BANNED_SUFFIXES)):
        raise ReleaseNetworkError('更新源仅支持不含账号密码的公开 HTTPS 域名（443）；禁止本地或内网地址')
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if (not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', hostname) or
                any(label.startswith('-') or label.endswith('-') for label in hostname.split('.'))):
            raise ReleaseNetworkError('更新源必须是有效公开域名，不能使用 IP 地址或主机别名')
    else:
        raise ReleaseNetworkError('更新源不允许直接填写 IP 地址，请使用公开 HTTPS 域名')
    if p.query and len(p.query)>8192:
        raise ReleaseNetworkError('下载地址查询参数过长')
    return hostname


def _https_url(url: str, *, api: bool=False, allowed_hosts=None) -> str:
    host = _hostname(url)
    allowed = _GITHUB_CDN if allowed_hosts is None else set(allowed_hosts) | _GITHUB_CDN
    if host not in allowed:
        raise ReleaseNetworkError('更新地址或重定向跳出了已配置的代理域名及可信 GitHub CDN')
    if api and host != 'api.github.com' and (
            allowed_hosts is None or host not in allowed_hosts):
        raise ReleaseNetworkError('Release API 必须来自已配置的公开 HTTPS 代理或 GitHub 官方 API')
    return url


@dataclass(frozen=True)
class Source:
    url: str
    owner: str
    repo: str
    origin: str
    prefix: str
    mode: str
    host: str

    def api_urls(self) -> tuple[str, ...]:
        tail = '/repos/' + self.owner + '/' + self.repo + '/releases/latest'
        if self.mode == 'github':
            return ('https://api.github.com' + tail,)
        if self.mode == 'full_proxy':
            return (self.origin + self.prefix + '/https://api.github.com' + tail,)
        # Different public reverse proxies use GitHub-compatible /api/repos,
        # or Gitea /api/v1/repos. Try the latter only when 404 is returned.
        return (self.origin + self.prefix + '/api' + tail,
                self.origin + self.prefix + '/api/v1' + tail)

    def asset_url(self, tag: str, name: str, browser_url: str) -> str:
        # Never use arbitrary URLs from JSON. Metadata must refer to precisely
        # this versioned filename and repository.
        base = '/releases/download/' + tag + '/' + name
        github = 'https://github.com/' + self.owner + '/' + self.repo + base
        local = self.url + base
        if browser_url not in (github, local):
            # Gitea's REST API may provide a numeric release asset URL.
            gitea = self.origin + self.prefix + '/api/v1/repos/' + self.owner + '/' + self.repo + '/releases/'
            if (self.mode != 'mirror' or
                    not browser_url.startswith(gitea) or
                    not re.fullmatch(r'[0-9]+/assets/[0-9]+', browser_url[len(gitea):])):
                raise ReleaseNetworkError('Release ZIP 下载地址与绑定仓库、版本或文件名不匹配')
            return _https_url(browser_url, allowed_hosts={self.host})
        if self.mode == 'full_proxy':
            return self.origin + self.prefix + '/https://github.com/' + self.owner + '/' + self.repo + base
        return local


def parse_source(url: str) -> Source:
    if not isinstance(url, str) or not (1 <= len(url.strip()) <= 300):
        raise ReleaseNetworkError('仓库地址必须是公开 HTTPS 地址（最多300字符）')
    raw = url.strip().rstrip('/')
    host = _hostname(raw)
    p = urllib.parse.urlsplit(raw)
    if p.query or p.fragment:
        raise ReleaseNetworkError('仓库地址不支持查询参数和片段')
    origin = 'https://' + host
    path = p.path
    embedded = '/https://github.com/'
    if embedded in path:
        prefix, tail = path.split(embedded, 1)
        if path.count(embedded) != 1:
            raise ReleaseNetworkError('代理仓库路径格式无效')
        parts = tail.split('/')
        mode = 'full_proxy'
    else:
        parts = path.strip('/').split('/')
        prefix = '/' + '/'.join(parts[:-2]) if len(parts)>2 else ''
        mode = 'github' if host == 'github.com' and not prefix else 'mirror'
        if mode == 'github' and len(parts)!=2:
            raise ReleaseNetworkError('GitHub 仓库地址必须是 /所有者/仓库')
    if (len(parts)!=2 or not _OWNER.fullmatch(parts[0]) or not _REPO.fullmatch(parts[1])
            or parts[1].lower().endswith('.git')):
        raise ReleaseNetworkError('请填写仓库根网址：HTTPS 镜像域名/所有者/仓库，或 HTTPS 代理/https://github.com/所有者/仓库')
    if prefix:
        for component in prefix.strip('/').split('/'):
            if not re.fullmatch(r'[A-Za-z0-9._~-]+',component) or component in ('.','..'):
                raise ReleaseNetworkError('代理地址前缀包含非法字符')
    return Source(url=raw, owner=parts[0], repo=parts[1],
                  origin=origin, prefix=prefix, mode=mode, host=host)


def canonical_repo(url: str) -> str:
    return parse_source(url).url


class _PinnedPublicHTTPSConnection(http.client.HTTPSConnection):
    """Resolve and connect directly to checked public IP with the real SNI."""
    def connect(self):
        hostname = self.host
        try:
            candidates = socket.getaddrinfo(hostname, self.port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ReleaseNetworkError('DNS 解析失败：' + hostname) from exc
        if not candidates:
            raise ReleaseNetworkError('DNS 未返回地址：' + hostname)
        if any(not ipaddress.ip_address(candidate[4][0]).is_global
               for candidate in candidates):
            raise ReleaseNetworkError('代理域名 DNS 指向内网或保留地址，已拒绝请求：' + hostname)
        last_error = None
        for family, socktype, proto, _, address in candidates:
            sock = socket.socket(family,socktype,proto)
            try:
                sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=hostname)
                return
            except OSError as exc:
                last_error=exc
                sock.close()
        raise last_error or ReleaseNetworkError('无法连接到公开 HTTPS 服务器')


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedPublicHTTPSConnection,req,context=self._context)


class _ScopedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = frozenset(hosts)
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_url(newurl, allowed_hosts=self.hosts)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def _opener(hosts):
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _PublicHTTPSHandler(),
        _ScopedRedirect(hosts))


def _describe_http(exc, host: str) -> str:
    code = int(exc.code)
    remaining = exc.headers.get('X-RateLimit-Remaining','') if exc.headers else ''
    if code==403 and remaining=='0':
        hint='GitHub API 匿名限流已耗尽'
    elif code==403:
        hint='访问被拒；可能代理禁止 API 或仓库访问受限'
    elif code==429:
        hint='API 请求频率过高，请稍后重试'
    elif code==404:
        hint='仓库不存在、没有正式 Release，或代理不支持此 API 路径'
    elif code in (502,503,504):
        hint='上游代理或 GitHub 服务暂时不可用'
    elif code in (401,):
        hint='公开仓库 API 需要认证，无法自动检查'
    else:
        hint='Release API 或下载服务返回 HTTP 错误'
    return 'HTTP '+str(code)+'（'+host+'）：'+hint


def _describe_network(exc: Exception, host: str) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return _describe_http(exc,host)
    reason = exc.reason if isinstance(exc,urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLError):
        return 'TLS 证书校验或握手失败（'+host+'），检查代理域名证书'
    if isinstance(reason, (TimeoutError,socket.timeout)):
        return '连接/读取超时（'+host+'），检查宿主机到代理的网络'
    if isinstance(reason, socket.gaierror):
        return 'DNS 解析失败（'+host+'），检查 DNS 和代理域名'
    if isinstance(reason, ConnectionRefusedError):
        return 'HTTPS 连接被拒绝（'+host+'）'
    if isinstance(reason, OSError):
        return '网络连接失败（'+host+'，'+type(reason).__name__+'），检查防火墙或路由'
    return 'HTTPS 请求失败（'+host+'，'+type(reason).__name__+'）'


def _read_json(url: str, hosts) -> dict:
    host = _hostname(url)
    _https_url(url,api=True,allowed_hosts=hosts)
    request = urllib.request.Request(url,headers={
        'User-Agent':'VM-Orchestrator-Portable-Release-Updater',
        'Accept':'application/vnd.github+json',
        'X-GitHub-Api-Version':'2022-11-28'})
    try:
        with _opener(hosts).open(request,timeout=18) as response:
            if int(response.headers.get('Content-Length') or '0') > _MAX_JSON:
                raise ReleaseNetworkError('Release API 响应大于2MB')
            blob=response.read(_MAX_JSON+1)
            if len(blob)>_MAX_JSON:
                raise ReleaseNetworkError('Release API 响应大于2MB')
            data=json.loads(blob.decode('utf-8'))
            if not isinstance(data,dict) or not isinstance(data.get('tag_name'),str):
                raise ReleaseNetworkError('代理未返回 GitHub/Gitea 兼容的 Release JSON；检查 API 路径')
            return data
    except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,OSError) as exc:
        raise ReleaseNetworkError(_describe_network(exc,host)) from exc
    except (json.JSONDecodeError,UnicodeDecodeError) as exc:
        raise ReleaseNetworkError('Release API 返回了非 JSON 内容，代理可能返回了网页') from exc


def fetch_release(repo_url: str) -> dict:
    source=parse_source(repo_url)
    problems=[]
    hosts={source.host}
    for i,url in enumerate(source.api_urls()):
        try:
            return _read_json(url,hosts)
        except ReleaseNetworkError as exc:
            problems.append(str(exc))
            # Only retry a different API path for a 404, never silently fall
            # back after a timeout, 403 or a malicious redirect.
            if 'HTTP 404' not in str(exc) or i+1==len(source.api_urls()):
                break
    raise ReleaseNetworkError('；'.join(problems)[:480])


def download_asset(url: str, destination: Path) -> tuple[str,int]:
    host=_hostname(url)
    _https_url(url,allowed_hosts={host})
    request=urllib.request.Request(url,headers={
        'User-Agent':'VM-Orchestrator-Portable-Release-Updater',
        'Accept':'application/octet-stream'})
    digest=hashlib.sha256()
    size=0
    try:
        with _opener({host}).open(request,timeout=70) as response:
            if int(response.headers.get('Content-Length') or '0')>_MAX_ZIP:
                raise ReleaseNetworkError('Release ZIP 超过512MB')
            with destination.open('xb') as output:
                while True:
                    block=response.read(1024*1024)
                    if not block:
                        break
                    size+=len(block)
                    if size>_MAX_ZIP:
                        raise ReleaseNetworkError('Release ZIP 超过512MB')
                    digest.update(block)
                    output.write(block)
    except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,OSError) as exc:
        raise ReleaseNetworkError('ZIP 下载失败：'+_describe_network(exc,host)) from exc
    if size<50:
        raise ReleaseNetworkError('下载内容小于50字节，不是有效的 Release ZIP')
    return digest.hexdigest(),size
