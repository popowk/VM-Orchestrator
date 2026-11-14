"""GitHub source verification must complete before activating or auto-delivering."""
from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from app.github_releases import (
    GitHubReleaseUpdater,GitHubSourceError,canonical_repo,safe_asset_name,
    _https_url,
)
from app.portable_catalog import PortableCatalog,profile_id_for
from app.fleet import FleetStore


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.fleet=FleetStore(self.root/'data'/'store.sqlite')
        self.catalog=PortableCatalog(self.fleet.path)
        self.profile={'id':profile_id_for('MyApp'),'display_name':'MyApp',
                      'folder':'MyApp','launcher':'Start.cmd',
                      'version_file':'','version_source':'zip:VERSION','version_override':''}
        self.catalog.upsert(self.profile)
        self.old=str(uuid.uuid4())
        self.fleet.add_package('portable-'+self.profile['id'],'1.0.0','a'*64,200,
                               'old.zip',self.old)
        self.catalog.record_package(self.old,self.profile)
        self.catalog.configure_github(self.profile['id'],
             'https://github.com/popowk/test-app',True,'')
        self.zip=self.root/'source.zip'
        with zipfile.ZipFile(self.zip,'w') as z:
            z.writestr('MyApp/Start.cmd','@echo off\n')
            z.writestr('MyApp/VERSION','1.1.3')
        self.sha=hashlib.sha256(self.zip.read_bytes()).hexdigest()

    def data(self,tag='v1.1.3'):
        return {'tag_name':tag,'draft':False,'prerelease':False,
                'assets':[{'name':'myapp-Windows-Full.zip',
                           'size':self.zip.stat().st_size,
                           'digest':'sha256:'+self.sha,
                           'browser_download_url':
                               'https://github.com/popowk/test-app/releases/download/'+tag+
                               '/myapp-Windows-Full.zip'}]}

    def downloader(self,url,path):
        shutil.copyfile(self.zip,path)
        return self.sha,self.zip.stat().st_size

    def updater(self,meta=None,downloader=None):
        return GitHubReleaseUpdater(self.catalog,self.fleet,
             get_release=lambda _:meta if meta is not None else self.data(),
             download=downloader or self.downloader)

    def test_versioned_zip_asset_filename_placeholder(self):
        self.assertEqual(
            safe_asset_name('myapp-v{version}-Windows-Full.zip'),
            'myapp-v{version}-Windows-Full.zip')
        self.catalog.configure_github(
            self.profile['id'], 'https://github.com/popowk/test-app', True,
            'myapp-v{version}-Windows-Full.zip')
        meta=self.data()
        meta['assets'][0]['name']='myapp-v1.1.3-Windows-Full.zip'
        meta['assets'][0]['browser_download_url']=meta['assets'][0]['browser_download_url'].replace(
            'myapp-Windows-Full.zip','myapp-v1.1.3-Windows-Full.zip')
        self.assertEqual(self.updater(meta).check(self.profile['id'])['state'],'published')

    def test_source_url_and_redirect_hosts_are_strict(self):
        self.assertEqual(canonical_repo('https://github.com/popowk/test-app/'),
                         'https://github.com/popowk/test-app')
        for url in ('http://github.com/popowk/test-app',
                    'https://github.com/popowk/test-app/releases/latest',
                    'https://github.com/popowk/test-app?x=1',
                    'https://github.com@evil.com/foo/bar',
                    'https://localhost/popowk/test-app',
                    'https://127.0.0.1/popowk/test-app',
                    'https://mirror.local/popowk/test-app'):
            with self.subTest(url=url),self.assertRaises(GitHubSourceError):
                canonical_repo(url)
        with self.assertRaises(GitHubSourceError):
            _https_url('https://release-assets.githubusercontent.com.evil.test/file')
        with self.assertRaises(GitHubSourceError):
            safe_asset_name('../script.zip')

    def test_first_verified_release_is_activated_once_not_duplicated(self):
        up=self.updater()
        result=up.check(self.profile['id'])
        self.assertEqual(result['state'],'published')
        self.assertEqual(result['version'],'1.1.3')
        self.assertEqual(result['auto_queued'],0)
        current=self.catalog.get(self.profile['id'])
        self.assertEqual(current['github_last_tag'],'v1.1.3')
        active=self.fleet.package(current['active_package_id'])
        self.assertEqual(active['sha256'],self.sha)
        self.assertEqual((self.fleet.path.parent/'approved_packages'/
                         (active['id']+'.zip')).read_bytes(),self.zip.read_bytes())
        again=up.check(self.profile['id'])
        self.assertEqual(again['state'],'unchanged')
        self.assertEqual(self.catalog.get(self.profile['id'])['active_package_id'],
                         active['id'])

    def test_corrupt_or_wrong_program_does_not_replace_active(self):
        with zipfile.ZipFile(self.zip,'w') as z:
            z.writestr('Other/Start.cmd','@echo off')
            z.writestr('Other/VERSION','1.1.3')
        self.sha=hashlib.sha256(self.zip.read_bytes()).hexdigest()
        before=self.catalog.get(self.profile['id'])['active_package_id']
        with self.assertRaisesRegex(GitHubSourceError,'顶层目录'):
            self.updater().check(self.profile['id'])
        self.assertEqual(self.catalog.get(self.profile['id'])['active_package_id'],before)
        self.assertTrue(self.catalog.get(self.profile['id'])['github_last_error'])

    def test_failed_version_mismatch_cannot_publish(self):
        meta=self.data()
        meta['tag_name']='v1.2.0'
        meta['assets'][0]['browser_download_url']=meta['assets'][0]['browser_download_url'].replace('v1.1.3','v1.2.0')
        with self.assertRaisesRegex(GitHubSourceError,'内部版本'):
            self.updater(meta).check(self.profile['id'])
        self.assertEqual(self.catalog.get(self.profile['id'])['active_package_id'],self.old)

    def test_zip_asset_url_must_match_configured_repo(self):
        meta=self.data()
        meta['assets'][0]['browser_download_url']='https://github.com/other/other/releases/download/v1.1.3/x.zip'
        with self.assertRaisesRegex(GitHubSourceError,'不匹配'):
            self.updater(meta).check(self.profile['id'])
        self.assertEqual(self.catalog.get(self.profile['id'])['active_package_id'],self.old)

    def test_source_disabled_by_default_and_does_not_disable_guest_push(self):
        second=profile_id_for('SecondApp')
        self.catalog.upsert({'id':second,'display_name':'SecondApp','folder':'SecondApp',
            'launcher':'Start.cmd','version_file':'','version_source':'auto','version_override':''})
        self.assertEqual(self.catalog.get(second)['github_enabled'],0)
        self.assertEqual(self.updater().check_enabled()['checked'],1)
        self.catalog.configure_github(self.profile['id'],
            'https://github.com/popowk/test-app',False,'')
        self.assertEqual(self.updater().check(self.profile['id'])['state'],'disabled')

    def test_true_opted_in_guest_push_follows_verified_release(self):
        with self.fleet.db() as d:
            d.execute('CREATE TABLE vms (id TEXT PRIMARY KEY,state TEXT)')
            d.execute("INSERT INTO vms VALUES('vm-a','READY')")
            from app.fleet import now
            d.execute('''INSERT INTO guest_enrollment
                (vm_id,agent_id,secret_hash,registered_at,last_seen,reported_json)
                VALUES(?,?,?,?,?,?)''',
                ('vm-a','agent-1','secret',now(),now(),'{"guest_agent":"2.0.27"}'))
        self.catalog.set_auto_deploy(self.profile['id'],True)
        self.assertEqual(self.updater().check(self.profile['id'])['auto_queued'],1)
        self.assertEqual(len(self.fleet.tasks('vm-a')),1)


if __name__=='__main__':
    unittest.main()
