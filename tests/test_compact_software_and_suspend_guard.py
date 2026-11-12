"""Release regressions for UI removal, portable-only update routing and safe VM state."""
import tempfile
import unittest
from pathlib import Path

from app.diagnostics import diagnose
from app.safety import assert_powered_off
from app.vmrun import VmrunError


ROOT = Path(__file__).resolve().parent.parent


class FakeVmrun:
    def __init__(self, running=False):
        self.running=running
    def is_running(self, vmx):
        return self.running


class SuspendedVmGuardTests(unittest.TestCase):
    def test_vmss_blocks_reinstall_even_if_vmrun_reports_not_running(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            vmx=root/'001.vmx'
            vmx.write_text('config.version = "8"\n', encoding='utf-8')
            (root/'001-2660ec3f.vmss').write_bytes(b'saved suspended memory')
            (root/'001.vmx.lck').mkdir()
            def probe_must_not_run(_):
                raise AssertionError('Exclusive-open cannot run when VM is suspended')
            report=diagnose(vmx,FakeVmrun(False),probe=probe_must_not_run)
            self.assertEqual(report['power_state'],'SUSPENDED')
            self.assertTrue(report['vmx_probe_skipped'])
            self.assertFalse(report['vmx_exclusive_open'])
            self.assertIn('正常关机',report['reason'])
            with self.assertRaisesRegex(VmrunError,'suspended'):
                assert_powered_off(vmx,FakeVmrun(False),strict_locks=True)
            self.assertTrue((root/'001-2660ec3f.vmss').exists())

    def test_off_state_is_not_enough_if_lock_marker_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); vmx=root/'001.vmx'
            vmx.write_text('test',encoding='ascii')
            (root/'001.vmx.lck').mkdir()
            report=diagnose(vmx,FakeVmrun(False),probe=lambda _:True)
            self.assertEqual(report['power_state'],'POWERED_OFF')
            self.assertFalse(report['vmx_probe_skipped'])
            with self.assertRaisesRegex(VmrunError,'lock markers'):
                assert_powered_off(vmx,FakeVmrun(False),strict_locks=True)


class StreamlinedUpdateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html=(ROOT/'app/ui/index.html').read_text(encoding='utf-8')
        cls.js=(ROOT/'app/ui/admin.js').read_text(encoding='utf-8')
        cls.main=(ROOT/'app/main.py').read_text(encoding='utf-8')
        cls.fleet=(ROOT/'app/fleet.py').read_text(encoding='utf-8')
        cls.runtime=(ROOT/'guest_agent/runtime_core.py').read_text(encoding='utf-8')

    def test_obsolete_vm_controls_and_modals_removed(self):
        for term in ('首次启动监控','一键验收','id="testDialog"','id="screenDialog"',
                     '查看屏幕截图','启用本机截图'):
            self.assertNotIn(term,self.html+self.js)
        self.assertNotIn('firstboot-start',self.main)
        self.assertNotIn('acceptance-test',self.main)
        self.assertIn('screen-preview.jpg',self.main)  # keep remote preview
        self.assertIn('screen.png',self.main)  # keep customer screenshot API

    def test_manual_enrollment_ui_and_routes_removed_but_auto_enrollment_remains(self):
        for value in ('下载专属安装包','创建一次性注册令牌',
                      '撤销注册','guest-installer.zip','guest-enrollment','revoke-guest'):
            self.assertNotIn(value,self.html+self.js+self.main)
        self.assertIn('def create_enrollment(self, vm_id:',self.fleet)
        self.assertIn('def revoke_vm(self, vm_id:',self.fleet)
        jobs=(ROOT/'app/jobs.py').read_text(encoding='utf-8')
        self.assertIn('fleet.create_enrollment(vm_id)',jobs)

    def test_compact_searchable_paginated_guest_list(self):
        for uid in ('softwareGuestSearch','softwareGuestFilter','softwareGuestPrev',
                    'softwareGuestNext','softwareGuestCount','softwareGuestPage'):
            self.assertIn('id="'+uid+'"',self.html)
        self.assertIn('pageSize:18',self.js)
        self.assertIn("el('details','software-guest-row')",self.js)
        self.assertIn("el('details','software-release')",self.js)
        self.assertIn('softwareGuestUI.expanded.add',self.js)
        self.assertIn('softwareGuestUI.releaseExpanded.add',self.js)

    def test_obsolete_adapters_not_callable_or_installed(self):
        for text in ('plugin-command-update','/api/fleet/packages/{product}/{version}',
                     'software-rollback/','guest-installer.zip'):
            self.assertNotIn(text,self.main)
        for text in ("'snowluma_direct'", "'dsh_plugin_user'", "'desktop_installer'",
                     "'portable_managed'", "'npm_global'"):
            self.assertNotIn(text,self.runtime)
        self.assertIn("options.get('mode')!='portable_catalog'",self.runtime)
        self.assertIn("if not portable:",self.fleet)
        self.assertIn('旧版安装适配器已停用',self.fleet)
        installer=(ROOT/'guest_agent/Install-GuestAgentV2.ps1').read_text(encoding='utf-8-sig')
        for filename in ('dsh_updates.py','quick_updates.py','quick_plugin_update.py',
                         'update_lifecycle.py','Configure-ManagedUpdates.ps1'):
            self.assertNotIn(filename,installer)
            self.assertFalse((ROOT/'guest_agent'/filename).exists())


if __name__=='__main__':
    unittest.main()
