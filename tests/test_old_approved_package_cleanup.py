"""Stale approved_packages ZIPs are visible, but referenced/current versions safe."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path

from app.cleanup import CleanupManager,CleanupError
from app.fleet import FleetStore
from app.portable_catalog import PortableCatalog,profile_id_for
from app.storage import TemplateStore


class FakeRun:
    def is_running(self,vmx):
        return False


class ApprovedPackageCleanupTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root=Path(t.name)/'VM-Orchestrator'
        self.root.mkdir()
        (self.root/'VERSION').write_text('1.4.62')
        self.customers=Path(t.name)/'Customers'
        self.customers.mkdir()
        self.db=self.root/'data'/'templates.sqlite3'
        self.store=TemplateStore(self.db)
        self.fleet=FleetStore(self.db)
        self.catalog=PortableCatalog(self.db)
        self.profile={'id':profile_id_for('MyApp'),'folder':'MyApp',
                      'launcher':'Start.cmd','display_name':'MyApp',
                      'version_file':'','version_source':'zip:VERSION','version_override':''}
        self.catalog.upsert(self.profile)
        self.packages=self.root/'data'/'approved_packages'
        self.packages.mkdir(parents=True,exist_ok=True)
        self.old=self.package('1.0.0')
        self.active=self.package('1.1.0')
        self.catalog.record_package(self.old,self.profile)
        self.catalog.record_package(self.active,self.profile)
        self.cleanup=CleanupManager(self.root,self.customers,self.db,
                                    self.store,self.fleet,FakeRun())

    def package(self, version):
        id=str(uuid.uuid4())
        data=(version+'\n').encode()*40
        digest=hashlib.sha256(data).hexdigest()
        self.fleet.add_package('portable-'+self.profile['id'],version,
                               digest,len(data),'build.zip',id)
        (self.packages/(id+'.zip')).write_bytes(data)
        return id

    def items(self):
        return self.cleanup.scan()['items']

    def test_old_version_visible_manual_and_current_version_protected(self):
        old=[item for item in self.items() if item['kind']=='old_software_package']
        self.assertEqual(len(old),1)
        self.assertEqual(old[0]['id'],'package:'+self.old)
        self.assertTrue(old[0]['manual'])
        scan=self.cleanup.scan()
        self.assertTrue(any(self.active in x['path'] for x in scan['protected']))
        self.assertEqual(scan['old_software_packages'],1)

    def test_requires_special_confirmation_and_catalog_tombstone(self):
        item=next(x for x in self.items() if x['kind']=='old_software_package')
        with self.assertRaisesRegex(CleanupError,'单独确认'):
            self.cleanup.delete([{'id':item['id'],'fingerprint':item['fingerprint']}])
        result=self.cleanup.delete([{'id':item['id'],
            'fingerprint':item['fingerprint'],'confirm_software':True}])
        self.assertEqual(result['deleted_bytes'],item['bytes'])
        self.assertFalse((self.packages/(self.old+'.zip')).exists())
        self.assertTrue((self.packages/(self.active+'.zip')).exists())
        with self.fleet.db() as d:
            old=d.execute('SELECT deleted_at FROM update_packages WHERE id=?',
                          (self.old,)).fetchone()
            history=d.execute('SELECT COUNT(*) FROM update_packages').fetchone()[0]
        self.assertIsNotNone(old['deleted_at'])
        self.assertEqual(history,2)

    def test_inflight_old_version_never_enter_delete_list(self):
        with self.fleet.db() as d:
            d.execute('''INSERT INTO guest_tasks(id,vm_id,package_id,status,created_at)
                         VALUES(?,?,?,?,?)''',
                      (str(uuid.uuid4()),'vm-a',self.old,'VERIFY_REQUIRED','2026-01-01'))
        scan=self.cleanup.scan()
        self.assertFalse(any(item['id']=='package:'+self.old for item in scan['items']))
        self.assertTrue(any(self.old in item['path'] for item in scan['protected']))

    def test_customer_self_service_pin_protects_zip(self):
        with self.fleet.db() as d:
            d.execute('CREATE TABLE business_self_actions(package_id TEXT)')
            d.execute('INSERT INTO business_self_actions VALUES(?)',(self.old,))
        scan=self.cleanup.scan()
        self.assertFalse(any(item['id']=='package:'+self.old for item in scan['items']))
        self.assertTrue(any('客户自助' in x['reason'] for x in scan['protected']))

    def test_unrecognized_zip_protected_not_deletable(self):
        unknown=self.packages/(str(uuid.uuid4())+'.zip')
        unknown.write_bytes(b'important data')
        scan=self.cleanup.scan()
        self.assertTrue(any(x['path']==str(unknown) for x in scan['protected']))
        self.assertFalse(any(x['path']==str(unknown) for x in scan['items']))

    def test_modified_checksum_blocks_deletion_even_when_size_unchanged(self):
        path=self.packages/(self.old+'.zip')
        payload=path.read_bytes()
        path.write_bytes(payload[::-1])
        item=next(x for x in self.items() if x['id']=='package:'+self.old')
        with self.assertRaisesRegex(CleanupError,'SHA-256'):
            self.cleanup.delete([{'id':item['id'],
                'fingerprint':item['fingerprint'],'confirm_software':True}])
        self.assertTrue(path.exists())
        self.assertIsNotNone(self.fleet.package(self.old))


if __name__=='__main__':
    unittest.main()
