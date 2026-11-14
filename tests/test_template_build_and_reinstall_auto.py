"""v1.4.62: isolated template manufacturing and per-registration auto-push."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.fleet import FleetStore,now
from app.portable_catalog import PortableCatalog,profile_id_for
from app.storage import TemplateStore
from app.template_build import (
    TemplateBuildManager,TemplateBuildError,isolate_roots,iso_vmx,
)


class FakeVmrun:
    def __init__(self, folder: Path):
        self.executable=folder/'vmrun.exe'
        self.executable.write_text('fake')
        (folder/'vmware-vdiskmanager.exe').write_text('fake')
        self.clones=[]
    def is_running(self, vmx):
        return False
    def clone_full(self, source, dest, name):
        assert source.is_file()
        assert dest.parent.is_dir()
        self.clones.append((str(source),str(dest)))
        dest.write_text('config.version = "8"\ncheckpoint.vmState = ""\n')
        (dest.parent/(name+'.vmdk')).write_bytes(b'isolated disk clone')


class TemplateBuilderTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root=Path(t.name)
        self.build=self.root/'VM-Orchestrator'/'template_build'
        self.templates=self.root/'Template'
        self.customers=self.root/'Customers'
        self.templates.mkdir()
        self.customers.mkdir()
        self.store=TemplateStore(self.root/'data'/'vm.sqlite')
        self.vmrun=FakeVmrun(self.root)
        self.pool=ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.pool.shutdown,wait=True)
        self.builder=TemplateBuildManager(
            self.build,self.templates,self.customers,self.store.db_path,
            self.store,self.vmrun,self.pool)

    def wait(self):
        self.pool.submit(lambda:None).result(timeout=10)

    def test_default_directories_are_disjoint(self):
        isolate_roots(self.build,self.templates,self.customers)
        with self.assertRaises(TemplateBuildError):
            isolate_roots(self.templates,self.templates,self.customers)
        with self.assertRaises(TemplateBuildError):
            isolate_roots(self.customers/'Build',self.templates,self.customers)

    def test_iso_new_disk_is_isolated_offline_and_no_customer_mutation(self):
        iso=self.root/'Windows10.iso';iso.write_bytes(b'fixture')
        def fake_run(args,**kwargs):
            Path(args[-1]).write_bytes(b'fresh empty VMDK')
            class Result:returncode=0
            return Result()
        with patch('app.template_build.subprocess.run',side_effect=fake_run):
            build=self.builder.create(name='Win10-ISO-01',mode='iso',
                                      iso_path=iso,memory_mb=8192,cpus=4,disk_gb=80)
            self.wait()
        data=self.builder.get(build['id'])
        self.assertEqual(data['status'],'READY',data['error'])
        self.assertTrue((self.build/'Win10-ISO-01'/'Win10-ISO-01.vmdk').exists())
        vmx=(self.build/'Win10-ISO-01'/'Win10-ISO-01.vmx').read_text()
        self.assertIn('ethernet0.present = "FALSE"',vmx)
        self.assertIn('ide1:0.fileName',vmx)
        self.assertEqual(list(self.templates.glob('**/*.vmx')),[])
        self.assertEqual(list(self.customers.glob('**/*.vmx')),[])
        with self.assertRaisesRegex(TemplateBuildError,'ISO'):
            self.builder.publish(build['id'])
        # Operator detached installer media in VMware settings and performed
        # the required Sysprep. The backend still publishes CANDIDATE only.
        path=self.build/'Win10-ISO-01'/'Win10-ISO-01.vmx'
        vmx=vmx.replace('ide1:0.fileName = "'+iso.as_posix()+'"',
                        'ide1:0.fileName = ""')
        path.write_text(vmx)
        self.builder.publish(build['id'])
        self.wait()
        result=self.builder.get(build['id'])
        self.assertEqual(result['status'],'PUBLISHED_CANDIDATE',result['error'])
        registered=self.store.get(result['official_template_id'])
        self.assertEqual(registered['status'],'CANDIDATE')
        self.assertTrue(Path(registered['vmx_path']).exists())
        self.assertTrue(path.exists())
        with self.assertRaises(TemplateBuildError):
            self.builder.publish(build['id'])

    def test_sealed_existing_template_full_clone_no_overwrite(self):
        folder=self.templates/'Original';folder.mkdir()
        source=folder/'Original.vmx'
        source.write_text('config.version = "8"\ncheckpoint.vmState = ""\n')
        (folder/'Original.vmdk').write_bytes(b'original golden disk')
        registered=self.store.add('Original',source)
        self.store.seal_attested(registered['id'],'operator',oobe_bootstrap=True,bootstrap_version=2)
        build=self.builder.create(name='Second',mode='clone',source_vmx=source)
        self.wait()
        self.assertEqual(self.builder.get(build['id'])['status'],'READY')
        self.builder.publish(build['id'])
        self.wait()
        result=self.builder.get(build['id'])
        self.assertEqual(result['status'],'PUBLISHED_CANDIDATE',result['error'])
        self.assertEqual(source.read_text(),'config.version = "8"\ncheckpoint.vmState = ""\n')
        self.assertEqual((folder/'Original.vmdk').read_bytes(),b'original golden disk')
        self.assertTrue((self.templates/'Second'/'Second.vmx').exists())
        self.assertEqual(self.store.get(registered['id'])['status'],'SEALED_ATTESTED')
        with self.assertRaises(TemplateBuildError):
            self.builder.create(name='Second',mode='clone',source_vmx=source)

    def test_unrecognized_build_folder_never_overwritten(self):
        folder=self.build/'Already'
        folder.mkdir()
        (folder/'important.txt').write_text('keep!')
        source=self.templates/'Simple.vmx'
        source.write_text('config.version = "8"\ncheckpoint.vmState = ""\n')
        with self.assertRaisesRegex(TemplateBuildError,'已存在'):
            self.builder.create(name='Already',mode='clone',source_vmx=source)
        self.assertEqual((folder/'important.txt').read_text(),'keep!')

    def test_guest_tools_exporter_present_in_release_source(self):
        root=Path(__file__).resolve().parent.parent
        self.assertTrue((root/'template_build'/'Export-GuestToolkit.ps1').is_file())
        self.assertTrue((root/'template_build'/'README.md').is_file())
        html=(root/'app/ui/index.html').read_text(encoding='utf-8')
        js=(root/'app/ui/admin.js').read_text(encoding='utf-8')
        self.assertIn('id="templateBuildForm"',html)
        self.assertIn('id="templateBuildList"',html)
        self.assertIn("request('/api/template-builds')",js)
        self.assertIn('renderTemplateBuilds()',js)


class FreshGuestGenerationTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root=Path(t.name)
        self.db=self.root/'fleet.sqlite'
        self.store=TemplateStore(self.db)
        self.fleet=FleetStore(self.db)
        self.catalog=PortableCatalog(self.db)
        with self.fleet.db() as d:
            d.execute("INSERT INTO vms(id,name,vmx_path,template_id,state,created_at) VALUES(?,?,?,?,?,?)",
                      ('vm-001','001',str(self.root/'Customers'/'001'/'001.vmx'),
                       'unused','READY',now()))
        self.profile={'id':profile_id_for('AutoTool'),'display_name':'AutoTool',
                      'folder':'AutoTool','launcher':'Start.cmd','version_file':'',
                      'version_source':'zip:VERSION','version_override':''}
        self.catalog.upsert(self.profile)
        self.pkg=str(uuid.uuid4())
        self.fleet.add_package('portable-'+self.profile['id'],
                               '1.1.2','a'*64,100,'auto.zip',self.pkg)
        self.catalog.record_package(self.pkg,self.profile)
        self.catalog.set_auto_deploy(self.profile['id'],True)

    def register(self):
        token=self.fleet.create_enrollment('vm-001')
        return self.fleet.enroll('vm-001',token)

    def test_new_windows_reinstalls_same_package_once_per_new_agent_identity(self):
        first=self.register()
        self.fleet.heartbeat('vm-001',{'guest_agent':'2.0.27','portable_software':{}})
        old=self.fleet.tasks('vm-001')
        self.assertEqual(len(old),1)
        self.assertEqual(old[0]['enrollment_agent_id'],first['agent_id'])
        self.fleet.heartbeat('vm-001',{'guest_agent':'2.0.27'})
        self.assertEqual(len(self.fleet.tasks('vm-001')),1)

        # REINSTALL replaces OS + revokes the old Agent, but VM ID stays.
        self.fleet.revoke_vm('vm-001')
        second=self.register()
        self.assertNotEqual(second['agent_id'],first['agent_id'])
        self.fleet.heartbeat('vm-001',{'guest_agent':'2.0.27','portable_software':{}})
        rows=self.fleet.tasks('vm-001')
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['enrollment_agent_id'],second['agent_id'])
        self.assertEqual(rows[0]['package_id'],self.pkg)
        self.assertEqual(rows[1]['enrollment_agent_id'],first['agent_id'])
        self.assertEqual(rows[1]['status'],'CANCELLED')
        task=self.fleet.claim('vm-001')
        self.assertEqual(task['id'],rows[0]['id'])
        self.assertEqual(task['options']['portable']['deploy_mode'],'install_or_update')
        self.assertTrue(task['options']['auto_deploy'])
        self.fleet.heartbeat('vm-001',{'guest_agent':'2.0.27'})
        self.assertEqual(len(self.fleet.tasks('vm-001')),2)

    def test_no_auto_push_when_policy_disabled(self):
        self.catalog.set_auto_deploy(self.profile['id'],False)
        self.register()
        self.fleet.heartbeat('vm-001',{'guest_agent':'2.0.27'})
        self.assertEqual(self.fleet.tasks('vm-001'),[])


if __name__=='__main__':
    unittest.main()
