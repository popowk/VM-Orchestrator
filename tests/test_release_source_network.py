"""Proxy/mirror Release URL safety, DNS pinning and actionable diagnostics."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
import urllib.error
import uuid
import zipfile
from unittest.mock import patch

from app.release_source_network import (
    ReleaseNetworkError,canonical_repo,parse_source,_https_url,_read_json,
    fetch_release,_PinnedPublicHTTPSConnection,_describe_network,
)
from app.github_releases import GitHubReleaseUpdater
from app.portable_catalog import PortableCatalog,profile_id_for
from app.fleet import FleetStore


class URLSafetyTests(unittest.TestCase):
    def test_supported_repository_urls_and_api_routes(self):
        github=parse_source('https://github.com/popowk/dsh-fish-soul-bridage')
        self.assertEqual(github.mode,'github')
        self.assertEqual(github.api_urls(),(
            'https://api.github.com/repos/popowk/dsh-fish-soul-bridage/releases/latest',))
        mirror=parse_source('https://git.wksite.cn/popowk/dsh-fish-soul-bridage')
        self.assertEqual(mirror.mode,'mirror')
        self.assertEqual(mirror.api_urls(),(
            'https://git.wksite.cn/api/repos/popowk/dsh-fish-soul-bridage/releases/latest',
            'https://git.wksite.cn/api/v1/repos/popowk/dsh-fish-soul-bridage/releases/latest'))
        proxy=parse_source(
            'https://gh-proxy.com/https://github.com/popowk/dsh-fish-soul-bridage')
        self.assertEqual(proxy.mode,'full_proxy')
        self.assertEqual(proxy.api_urls(),(
            'https://gh-proxy.com/https://api.github.com/repos/popowk/dsh-fish-soul-bridage/releases/latest',))

    def test_mirror_rewrites_github_asset_but_never_arbitrary_assets(self):
        mirror=parse_source('https://git.wksite.cn/popowk/example')
        url='https://github.com/popowk/example/releases/download/v1.2.0/Windows-Full.zip'
        self.assertEqual(mirror.asset_url('v1.2.0','Windows-Full.zip',url),
                         'https://git.wksite.cn/popowk/example/releases/download/v1.2.0/Windows-Full.zip')
        proxy=parse_source('https://proxy.example/https://github.com/popowk/example')
        self.assertEqual(proxy.asset_url('v1.2.0','Windows-Full.zip',url),
                         'https://proxy.example/https://github.com/popowk/example/releases/download/v1.2.0/Windows-Full.zip')
        with self.assertRaisesRegex(ReleaseNetworkError,'不匹配'):
            mirror.asset_url('v1.2.0','Windows-Full.zip',
                'https://evil.example/other/releases/download/v1.2.0/Windows-Full.zip')
        with self.assertRaisesRegex(ReleaseNetworkError,'不匹配'):
            mirror.asset_url('v1.2.0','Windows-Full.zip',
                'https://github.com/popowk/example/releases/download/v2.0/Windows-Full.zip')

    def test_local_ips_and_non_https_hosts_are_rejected(self):
        invalid=[
            'http://git.wksite.cn/popowk/app',
            'https://127.0.0.1/popowk/app',
            'https://[::1]/popowk/app',
            'https://localhost/popowk/app',
            'https://mirror.local/popowk/app',
            'https://mirror.internal/popowk/app',
            'https://user:secret@mirror.example/popowk/app',
            'https://mirror.example:8080/popowk/app',
            'https://mirror.example/popowk/app?token=abc',
            'https://mirror.example/popowk/app/../../localhost',
        ]
        for url in invalid:
            with self.subTest(url=url),self.assertRaises(ReleaseNetworkError):
                canonical_repo(url)

    def test_redirect_bound_to_public_proxy_and_github_cdn(self):
        _https_url('https://git.wksite.cn/owner/repo',allowed_hosts={'git.wksite.cn'})
        _https_url('https://release-assets.githubusercontent.com/abc?signature=x',
                   allowed_hosts={'git.wksite.cn'})
        with self.assertRaises(ReleaseNetworkError):
            _https_url('https://192.168.2.1/admin',
                       allowed_hosts={'192.168.2.1'})
        with self.assertRaises(ReleaseNetworkError):
            _https_url('https://evil.example/download',allowed_hosts={'git.wksite.cn'})

    def test_dns_pinning_blocks_private_results_before_connect(self):
        with patch('app.release_source_network.socket.getaddrinfo',
                  return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',
                                 ('10.10.10.1',443))]):
            obj=_PinnedPublicHTTPSConnection('git.wksite.cn',timeout=5)
            with self.assertRaisesRegex(ReleaseNetworkError,'内网或保留'):
                obj.connect()

    def test_diagnostic_details_distinguish_http_and_network(self):
        for code,expected in [(403,'访问被拒'),(404,'不存在'),(429,'频率'),
                              (502,'暂时不可用')]:
            error=urllib.error.HTTPError('https://proxy.example',code,'reason',{},None)
            self.assertIn(expected,_describe_network(error,'proxy.example'))
        limited=urllib.error.HTTPError(
            'https://api.github.com',403,'reason',{'X-RateLimit-Remaining':'0'},None)
        self.assertIn('限流',_describe_network(limited,'api.github.com'))
        dns=urllib.error.URLError(socket.gaierror(socket.EAI_NONAME,'name missing'))
        self.assertIn('DNS',_describe_network(dns,'git.wksite.cn'))
        timeout=urllib.error.URLError(TimeoutError('timed out'))
        self.assertIn('超时',_describe_network(timeout,'git.wksite.cn'))

    def test_git_compatible_api_mirror_tries_second_path_on_404(self):
        calls=[]
        def handler(url,hosts):
            calls.append(url)
            if url.endswith('/api/repos/owner/app/releases/latest'):
                raise ReleaseNetworkError('HTTP 404（mirror.example）：不存在')
            return {'tag_name':'v1.0.0','assets':[]}
        with patch('app.release_source_network._read_json',side_effect=handler):
            data=fetch_release('https://mirror.example/owner/app')
        self.assertEqual(data['tag_name'],'v1.0.0')
        self.assertEqual(len(calls),2)
        self.assertIn('/api/v1/repos/',calls[-1])


class MirrorUpdaterTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        root=Path(t.name)
        self.fleet=FleetStore(root/'data'/'db.sqlite')
        self.catalog=PortableCatalog(self.fleet.path)
        self.profile={'id':profile_id_for('App'),'display_name':'App',
            'folder':'App','launcher':'Start.cmd','version_file':'',
            'version_source':'auto','version_override':''}
        self.catalog.upsert(self.profile)
        self.old=str(uuid.uuid4())
        self.fleet.add_package('portable-'+self.profile['id'],
            '1.0.0','a'*64,100,'old.zip',self.old)
        self.catalog.record_package(self.old,self.profile)
        self.archive=root/'release.zip'
        with zipfile.ZipFile(self.archive,'w') as z:
            z.writestr('App/Start.cmd','@echo off')
            z.writestr('App/VERSION','1.1.3')
        self.digest=hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_successful_proxy_rewrites_release_zip_download(self):
        url='https://proxy.example/https://github.com/popowk/app'
        self.catalog.configure_github(self.profile['id'],url,True,'')
        actual=[]
        def fetch(_):
            return {'tag_name':'v1.1.3','draft':False,'prerelease':False,
                    'assets':[{'name':'App-v1.1.3-Windows-Full.zip',
                        'size':self.archive.stat().st_size,
                        'digest':'sha256:'+self.digest,
                        'browser_download_url':'https://github.com/popowk/app/releases/download/v1.1.3/App-v1.1.3-Windows-Full.zip'}]}
        def download(release_url,target):
            actual.append(release_url)
            shutil.copyfile(self.archive,target)
            return self.digest,self.archive.stat().st_size
        updater=GitHubReleaseUpdater(self.catalog,self.fleet,
                                     get_release=fetch,download=download)
        self.assertEqual(updater.check(self.profile['id'])['state'],'published')
        self.assertEqual(actual,[
            'https://proxy.example/https://github.com/popowk/app/releases/download/v1.1.3/App-v1.1.3-Windows-Full.zip'])
        self.assertNotEqual(self.catalog.get(self.profile['id'])['active_package_id'],self.old)
        self.assertEqual(updater.check(self.profile['id'])['state'],'unchanged')
        self.assertEqual(len(actual),1)

    def test_public_mirror_can_download_github_origin_asset(self):
        source='https://git.wksite.cn/popowk/app'
        self.catalog.configure_github(self.profile['id'],source,True,'')
        seen=[]
        def download(url,target):
            seen.append(url)
            shutil.copyfile(self.archive,target)
            return self.digest,self.archive.stat().st_size
        updater=GitHubReleaseUpdater(self.catalog,self.fleet,
            get_release=lambda _:{'tag_name':'v1.1.3',
               'assets':[{'name':'app.zip','size':self.archive.stat().st_size,
                 'browser_download_url':'https://github.com/popowk/app/releases/download/v1.1.3/app.zip'}]},
            download=download)
        self.assertEqual(updater.check(self.profile['id'])['state'],'published')
        self.assertEqual(seen,[
            'https://git.wksite.cn/popowk/app/releases/download/v1.1.3/app.zip'])


if __name__=='__main__':
    unittest.main()
