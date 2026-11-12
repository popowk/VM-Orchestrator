"""Portable program catalog and state-preserving updater regression tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
import zipfile

from app.portable_catalog import (PortableCatalog, PortableCatalogError,
                                  inspect_zip, relative_file, profile_id_for)
from guest_agent.portable_software import update_portable, installed_version
from guest_agent.runtime_core import safe_extract_zip


def make_zip(path, folder='MyApp', version='1.2.3', *, extras=None):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr(f'{folder}/', '')
        z.writestr(f'{folder}/MyApp.exe', b'MZ harmless test fixture')
        z.writestr(f'{folder}/VERSION', version)
        for name, content in (extras or {}).items():
            z.writestr(name, content)


class ArchiveTests(unittest.TestCase):
    def test_only_one_folder_and_launcher_required(self):
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp)/'new.zip'
            make_zip(archive)
            info=inspect_zip(archive,'MyApp.exe',display_name='测试应用')
            self.assertEqual(info['folder'],'MyApp')
            self.assertEqual(info['launcher'],'MyApp.exe')
            self.assertEqual(info['version'],'1.2.3')
            self.assertEqual(info['version_source'],'zip:VERSION')
            self.assertEqual(info['id'],profile_id_for('MyApp'))
            with self.assertRaises(PortableCatalogError):
                inspect_zip(archive,'Missing.exe',display_name='MyApp')
            with self.assertRaises(PortableCatalogError):
                inspect_zip(archive,'../MyApp.exe',display_name='MyApp')

    def test_ambiguous_or_unsafe_zip_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp)/'extra.zip'
            make_zip(archive,extras={'OtherApp/outside.exe':b'more'})
            with self.assertRaises(PortableCatalogError):
                inspect_zip(archive,'MyApp.exe',display_name='MyApp')
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('../outside.exe',b'evil')
                z.writestr('MyApp/MyApp.exe',b'ok')
            with self.assertRaises(PortableCatalogError):
                inspect_zip(archive,'MyApp.exe',display_name='MyApp')

    def test_manual_version_or_opaque_hash_when_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            zip_path=Path(temp)/'new.zip'
            with zipfile.ZipFile(zip_path,'w') as z:
                z.writestr('MyApp/MyApp.exe',b'MZ portable')
            manual=inspect_zip(zip_path,'MyApp.exe',display_name='MyApp',
                               version_override='2.5.1')
            self.assertEqual(manual['version'],'2.5.1')
            self.assertEqual(manual['version_source'],'manual')
            detected=inspect_zip(zip_path,'MyApp.exe',display_name='MyApp')
            self.assertTrue(detected['version'].startswith('build-'))
            self.assertEqual(detected['version_source'],'zip_sha256')
            self.assertNotEqual(manual['version'],detected['version'])

    def test_catalog_remembers_launcher_and_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'db.sqlite3'
            catalog=PortableCatalog(path)
            data={'id':profile_id_for('MyApp'),'display_name':'MyApp','folder':'MyApp',
                  'launcher':'MyApp.exe','version_file':'','version_source':'zip:VERSION',
                  'version_override':''}
            catalog.upsert(data)
            catalog.record_package(str(uuid.uuid4()),data)
            self.assertEqual(catalog.get(data['id'])['launcher'],'MyApp.exe')
            self.assertEqual(len(catalog.all()),1)
            changed={**data,'launcher':'Surprise.exe'}
            with self.assertRaises(PortableCatalogError):
                catalog.upsert(changed)


class GuestPortableUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.base=root/'dsh';self.base.mkdir()
        self.working=root/'agent'
        self.program=self.base/'MyApp'
        self.program.mkdir()
        (self.program/'MyApp.exe').write_bytes(b'MZ old')
        (self.program/'VERSION').write_text('1.0.0')
        (self.program/'data').mkdir()
        (self.program/'data'/'save.txt').write_text('customer data')
        self.archive=root/'new.zip'
        make_zip(self.archive,version='1.2.3',
                 extras={'MyApp/data/save.txt':'ZIP bundled stale data'})
        self.profile={'folder':'MyApp','launcher':'MyApp.exe',
                      'version_source':'zip:VERSION','version_file':''}
        self.task_id=str(uuid.uuid4())
        self.modes=[]

    def update(self, checker, launch):
        return update_portable({'portable':self.profile},self.archive,
               self.task_id,'1.2.3',self.working,extractor=safe_extract_zip,
               on_phase=self.modes.append,base=self.base,
               process_inspector=checker,launcher=launch)

    def test_not_running_stays_stopped_and_preserves_customer_data(self):
        calls=[]
        output=self.update(lambda mode: calls.append(mode) or {'running': False},
                           lambda: self.fail('should never launch stopped application'))
        self.assertFalse(output['previous_running'])
        self.assertFalse(output['restarted'])
        self.assertEqual(calls,['Inspect'])
        self.assertEqual((self.program/'VERSION').read_text(),'1.2.3')
        self.assertEqual((self.program/'data'/'save.txt').read_text(),'customer data')
        self.assertTrue(Path(output['backup']).is_dir())
        self.assertEqual((Path(output['backup'])/'VERSION').read_text(),'1.0.0')
        self.assertIn('verified',self.modes)

    def test_started_app_stops_and_restarts_only_after_swap(self):
        state={'running':True}
        calls=[]
        def process(mode):
            calls.append(mode)
            if mode=='Stop':state['running']=False
            return {'running':state['running']}
        def launch():
            self.assertEqual((self.program/'VERSION').read_text(),'1.2.3')
            calls.append('Launch')
            state['running']=True
        result=self.update(process,launch)
        self.assertTrue(result['restarted'])
        self.assertEqual(calls[:3],['Inspect','Stop','Inspect'])
        self.assertIn('Launch',calls)
        self.assertIn('healthy',self.modes)

    def test_launch_failure_rolls_back_old_install(self):
        state={'running':True}
        def process(mode):
            if mode=='Stop':state['running']=False
            return {'running':state['running']}
        starts=[]
        def launch():
            starts.append((self.program/'VERSION').read_text())
            if len(starts)==1:
                raise RuntimeError('new app cannot start')
            state['running']=True
        with self.assertRaisesRegex(RuntimeError,'new app cannot start'):
            self.update(process,launch)
        self.assertEqual(starts,['1.2.3','1.0.0'])
        self.assertEqual((self.program/'VERSION').read_text(),'1.0.0')
        self.assertIn('rolled_back_healthy',self.modes)


if __name__ == '__main__':
    unittest.main()
