"""v1.4.58: one active release per program, one-time auto-delivery and customer screen."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from app.fleet import FleetStore, FleetError, now
from app.portable_catalog import PortableCatalog, PortableCatalogError, profile_id_for

ROOT=Path(__file__).resolve().parent.parent


class AutomaticPortableRolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'fleet.sqlite3'
        self.fleet=FleetStore(self.db)
        # The production TemplateStore owns this table.
        with self.fleet.db() as d:
            d.execute('CREATE TABLE vms (id TEXT PRIMARY KEY, state TEXT NOT NULL)')
            d.execute("INSERT INTO vms VALUES('vm-a','READY')")
            d.execute("INSERT INTO vms VALUES('vm-b','READY')")
            d.execute('''INSERT INTO guest_enrollment
                (vm_id,agent_id,secret_hash,registered_at,last_seen,reported_json)
                VALUES(?,?,?,?,?,?)''',
                ('vm-a','agent-a','secret-a',now(),now(),json.dumps({'guest_agent':'2.0.27'})))
            d.execute('''INSERT INTO guest_enrollment
                (vm_id,agent_id,secret_hash,registered_at,last_seen,reported_json)
                VALUES(?,?,?,?,?,?)''',
                ('vm-b','agent-b','secret-b',now(),None,json.dumps({'guest_agent':'2.0.27'})))
        self.catalog=PortableCatalog(self.db)
        self.profile={'id':profile_id_for('MyApp'), 'display_name':'MyApp',
                      'folder':'MyApp','launcher':'MyApp.exe','version_file':'',
                      'version_source':'zip:VERSION','version_override':''}
        self.catalog.upsert(self.profile)
        self.p1=str(uuid.uuid4())
        self.fleet.add_package('portable-'+self.profile['id'],'1.0.0',
                               'a'*64,100,'approved-portable-update.zip',self.p1)
        self.catalog.record_package(self.p1,self.profile)

    def tasks(self,vm='vm-a'):
        return self.fleet.tasks(vm)

    def test_default_policy_is_off_and_single_active_release(self):
        data=self.catalog.get(self.profile['id'])
        self.assertEqual(data['auto_deploy'],0)
        self.assertEqual(data['active_package_id'],self.p1)
        self.assertEqual(self.fleet.reconcile_auto_deploy()['queued'],[])
        self.assertEqual(self.tasks(),[])

    def test_opt_in_delivers_once_and_heartbeat_never_retries(self):
        self.catalog.set_auto_deploy(self.profile['id'],True)
        result=self.fleet.reconcile_auto_deploy()
        self.assertEqual(len(result['queued']),1)
        self.assertEqual(result['queued'][0]['vm_id'],'vm-a')
        for _ in range(3):
            self.fleet.heartbeat('vm-a',{'guest_agent':'2.0.27'})
        self.assertEqual(len(self.tasks()),1)
        self.assertEqual(self.tasks()[0]['package_id'],self.p1)
        self.assertEqual(self.tasks()[0]['status'],'QUEUED')
        self.assertEqual(len(self.fleet.tasks('vm-b')),0)

    def test_guest_first_online_receives_latest_version_once(self):
        self.catalog.set_auto_deploy(self.profile['id'],True)
        self.fleet.reconcile_auto_deploy()
        self.fleet.heartbeat('vm-b',{'guest_agent':'2.0.27'})
        self.assertEqual(len(self.tasks('vm-b')),1)
        self.fleet.heartbeat('vm-b',{'guest_agent':'2.0.27'})
        self.assertEqual(len(self.tasks('vm-b')),1)

    def test_new_release_supersedes_queued_old_version(self):
        self.catalog.set_auto_deploy(self.profile['id'],True)
        self.fleet.reconcile_auto_deploy()
        p2=str(uuid.uuid4())
        self.fleet.add_package('portable-'+self.profile['id'],'1.1.0',
                               'b'*64,110,'approved-portable-update.zip',p2)
        self.catalog.record_package(p2,self.profile)
        self.assertEqual(self.catalog.get(self.profile['id'])['active_package_id'],p2)
        self.assertEqual(self.tasks()[0]['status'],'CANCELLED')
        with self.assertRaisesRegex(FleetError,'生效版本'):
            self.fleet.queue_update('vm-a',self.p1)
        self.fleet.reconcile_auto_deploy()
        task=self.fleet.claim('vm-a')
        self.assertEqual(task['package_id'],p2)
        self.assertEqual(len(self.tasks()),2)
        self.assertEqual(self.tasks()[1]['status'],'CANCELLED')

    def test_turning_off_auto_cancels_only_queued_auto_tasks(self):
        self.catalog.set_auto_deploy(self.profile['id'],True)
        self.fleet.reconcile_auto_deploy()
        self.catalog.set_auto_deploy(self.profile['id'],False)
        self.assertEqual(self.tasks()[0]['status'],'CANCELLED')
        self.fleet.heartbeat('vm-a',{'guest_agent':'2.0.27'})
        self.assertEqual(len(self.tasks()),1)
        self.assertEqual(self.catalog.get(self.profile['id'])['auto_deploy'],0)

    def test_same_software_name_cannot_reference_another_folder(self):
        other={**self.profile,
               'id':profile_id_for('OtherFolder'),'folder':'OtherFolder',
               'launcher':'Other.exe'}
        with self.assertRaisesRegex(PortableCatalogError,'软件名称已绑定'):
            self.catalog.upsert(other)

    def test_deleting_active_package_turns_off_auto(self):
        self.catalog.set_auto_deploy(self.profile['id'],True)
        self.fleet.delete_package(self.p1)
        result=self.catalog.get(self.profile['id'])
        self.assertFalse(result['auto_deploy'])
        self.assertEqual(result['active_package_id'],'')


class CustomerScreenTests(unittest.TestCase):
    def test_screen_and_controls_are_one_grid(self):
        html=(ROOT/'app/ui/client.html').read_text('utf-8')
        css=(ROOT/'app/ui/client.css').read_text('utf-8')
        js=(ROOT/'app/ui/client.js').read_text('utf-8')
        a=html.index('class="customer-live-layout"')
        b=html.index('class="customer-actions-history"')
        layout=html[a:b]
        self.assertIn('class="screen-panel"',layout)
        self.assertIn('class="customer-live-controls"',layout)
        self.assertLess(layout.index('实时屏幕截图'),layout.index('电源操作'))
        self.assertLess(layout.index('电源操作'),layout.index('系统重装'))
        self.assertLess(layout.index('系统重装'),layout.index('快照管理'))
        self.assertIn('grid-template-columns:minmax(0,4fr) minmax(235px,1fr)',css)
        self.assertIn('aspect-ratio:16/9',css)
        self.assertIn("empty.hidden=true",js)
        self.assertIn("#screenEmpty[hidden]{display:none!important}",css)

    def test_admin_autopush_switch_and_one_active_filter(self):
        js=(ROOT/'app/ui/admin.js').read_text('utf-8')
        main=(ROOT/'app/main.py').read_text('utf-8')
        self.assertIn('software-auto-control',js)
        self.assertIn("'auto-deploy','POST',{enabled,confirm_all:enabled}",js)
        self.assertIn("if item['product'].startswith('portable-p-') and item['id'] in active",main)
        self.assertIn("previous['sha256']==checksum.hexdigest()",main)
        self.assertIn("previous['version']==profile['version']",main)


if __name__=='__main__':
    unittest.main()
