"""Regression tests: new portable installs, VMX suspend evidence and customer layout."""
from __future__ import annotations

import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from guest_agent.portable_software import update_portable
from guest_agent.runtime_core import safe_extract_zip
from app.power_state import suspend_evidence, reported_power_state
from app.safety import assert_powered_off
from app.vmrun import VmrunError

ROOT=Path(__file__).resolve().parent.parent


class GuestFreshDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.base=self.root/'dsh'
        self.base.mkdir()
        self.archive=self.root/'build.zip'
        with zipfile.ZipFile(self.archive,'w') as z:
            z.writestr('MyApp/MyApp.exe',b'MZ test fixture')
            z.writestr('MyApp/VERSION','2.1.0')
        self.profile={'portable':{'folder':'MyApp','launcher':'MyApp.exe',
                                   'version_source':'zip:VERSION','version_file':''}}

    def run_deploy(self, **overrides):
        return update_portable(self.profile,self.archive,str(uuid.uuid4()),'2.1.0',
                               self.root/'agent',extractor=safe_extract_zip,
                               base=self.base,**overrides)

    def test_missing_program_is_installed_without_launch(self):
        self.assertFalse((self.base/'MyApp').exists())
        result=self.run_deploy(
            process_inspector=lambda mode: self.fail('Fresh deploy must not stop any process'),
            launcher=lambda: self.fail('Fresh deploy must not automatically launch'))
        self.assertTrue(result['installed_fresh'])
        self.assertFalse(result['previous_running'])
        self.assertFalse(result['restarted'])
        self.assertEqual(result['backup'],'')
        self.assertEqual((self.base/'MyApp'/'VERSION').read_text(),'2.1.0')
        self.assertIn('首次安装',result['note'])
        self.assertFalse(any(self.base.glob('.vmo-deploy-owner-*')))
        self.assertFalse(any(self.base.glob('.vmo-deploy-lock-*')))

    def test_unrecognized_existing_directory_is_never_overwritten(self):
        path=self.base/'MyApp';path.mkdir()
        (path/'important.data').write_text('do not destroy')
        with self.assertRaisesRegex(RuntimeError,'UNKNOWN_EXISTING_DIRECTORY'):
            self.run_deploy()
        self.assertEqual((path/'important.data').read_text(),'do not destroy')

    def test_bad_new_version_quarantines_owned_install_without_launch(self):
        with self.assertRaisesRegex(RuntimeError,'INSTALLED_VERSION_MISMATCH'):
            update_portable(self.profile,self.archive,str(uuid.uuid4()),'9.0.0',
                            self.root/'agent',extractor=safe_extract_zip,base=self.base,
                            process_inspector=lambda mode:self.fail('No old processes'),
                            launcher=lambda:self.fail('Never launch a fresh install'))
        self.assertFalse((self.base/'MyApp').exists())
        self.assertEqual(len(list(self.base.glob('.vmo-failed-MyApp-*'))),1)

    def test_zip_target_does_not_escape_c_dsh(self):
        profile={'portable':{'folder':'../outside','launcher':'MyApp.exe',
                             'version_source':'zip:VERSION'}}
        with self.assertRaisesRegex(RuntimeError,'FOLDER_INVALID'):
            update_portable(profile,self.archive,str(uuid.uuid4()),'2.1.0',
                            self.root/'agent',extractor=safe_extract_zip,base=self.base)


class FakeVmrun:
    def is_running(self, vmx):
        return False


class VmPowerEvidenceTests(unittest.TestCase):
    def test_old_vmss_without_vmx_reference_is_not_reported_as_suspended(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            vmx=root/'001.vmx'
            vmx.write_text('checkpoint.vmState = ""\n',encoding='utf-8')
            (root/'001-old.vmss').write_bytes(b'old')
            evidence=suspend_evidence(vmx)
            self.assertEqual(evidence['state'],'residual')
            self.assertEqual(reported_power_state(vmx,False),'POWERED_OFF')
            self.assertTrue(evidence['warning'])
            with self.assertRaisesRegex(VmrunError,'suspended'):
                assert_powered_off(vmx,FakeVmrun(),strict_locks=True)

    def test_active_vmx_checkpoint_still_requires_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            vmx=root/'001.vmx'
            vmx.write_text('checkpoint.vmState = "001-2026.vmss"\n')
            (root/'001-2026.vmss').write_bytes(b'active checkpoint')
            self.assertEqual(suspend_evidence(vmx)['state'],'suspended')
            self.assertEqual(reported_power_state(vmx,False),'SUSPENDED')

    def test_missing_checkpoint_file_is_unknown_not_safe_off(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            vmx=root/'001.vmx'
            vmx.write_text('checkpoint.vmState = "missing.vmss"\n')
            self.assertEqual(reported_power_state(vmx,False),'UNKNOWN')


class UserInterfaceTests(unittest.TestCase):
    def test_customer_screen_layout_and_actions(self):
        html=(ROOT/'app/ui/client.html').read_text(encoding='utf-8')
        css=(ROOT/'app/ui/client.css').read_text(encoding='utf-8')
        js=(ROOT/'app/ui/client.js').read_text(encoding='utf-8')
        self.assertIn('screen-hint',html)
        self.assertIn('aspect-ratio:16/9',css)
        self.assertLess(html.index('customer-power-reinstall'),html.index('快照管理'))
        self.assertLess(html.index('快照管理'),html.index('customer-actions-history'))
        self.assertIn('重装系统快照丢失',html)
        self.assertIn('customer-history-panel',html)
        self.assertIn('items.slice(0,2)',js)
        self.assertNotIn("'已启动':",js)
        self.assertIn("row.append(body)",js)
        self.assertIn('suspend_reinstall_blocked',js)

    def test_admin_recent_five_and_one_or_all(self):
        js=(ROOT/'app/ui/admin.js').read_text(encoding='utf-8')
        self.assertIn("'__ALL__'",js)
        self.assertIn("scope:all?'all':'one'",js)
        self.assertIn("merged.slice((page-1)*5,page*5)",js)
        self.assertIn("loadRecentOps(vm,page)",js)
        self.assertIn('remote-self-controls',js)
        self.assertIn('每页5条',js)
        self.assertNotIn("return Object.keys(g.reported?.portable_software||{}).some",js)

    def test_backend_queue_allows_missing_program_with_new_agent(self):
        fleet=(ROOT/'app/fleet.py').read_text(encoding='utf-8')
        main=(ROOT/'app/main.py').read_text(encoding='utf-8')
        self.assertIn("options['portable']['deploy_mode']='install_or_update'",fleet)
        self.assertIn('parts<(2,0,27)',fleet)
        self.assertIn("'/api/fleet/portable/deploy'",main)
        self.assertIn('payload.confirm_all',main)


if __name__=='__main__':
    unittest.main()
