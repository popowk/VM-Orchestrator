"""Regression checks: resume a suspended VM and show client presets in remote admin."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from app.vmrun import Vmrun


class SuspendedStartTests(unittest.TestCase):
    def test_job_start_branch_keeps_suspended_disk_state(self):
        source=Path('app/jobs.py').read_text(encoding='utf-8')
        start=source.index("elif command == 'START':")
        stop=source.index("elif command == 'STOP':",start)
        block=source[start:stop]
        self.assertIn('self.vmrun.start(vmx)',block)
        self.assertNotIn('unlink(',block)
        self.assertNotIn('rmtree(',block)
        self.assertNotIn('snapshot_restore',block)

    def test_vmrun_uses_normal_start_command_to_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            exe=Path(directory)/'vmrun.exe';exe.write_bytes(b'test')
            vmx=Path(directory)/'guest.vmx'
            with patch('app.vmrun.subprocess.run',
                       return_value=SimpleNamespace(returncode=0,stdout='',stderr='')) as invoke:
                Vmrun(exe).start(vmx)
            args=invoke.call_args.args[0]
            self.assertEqual(args[1:4],['-T','ws','start'])
            self.assertEqual(args[-1],'nogui')

    def test_suspend_start_is_enabled_in_vm_management(self):
        js=Path('app/ui/admin.js').read_text(encoding='utf-8')
        self.assertIn("vm.power_state==='SUSPENDED'?'恢复运行':'开机'",js)
        self.assertIn("!['POWERED_OFF','SUSPENDED'].includes(vm.power_state)",js)
        self.assertIn("vmAction(vm,'start')",js)


class RemoteCustomerActionsTests(unittest.TestCase):
    def test_self_panel_is_between_command_and_file_log_panels(self):
        html=Path('app/ui/index.html').read_text(encoding='utf-8')
        self.assertLess(html.index('<h2>远程命令</h2>'),
                        html.index('id="remoteSelfActionsPanel"'))
        self.assertLess(html.index('id="remoteSelfActionsPanel"'),
                        html.index('<h2>文件与日志</h2>'))
        self.assertIn('id="remoteSelfActions"',html)

    def test_admin_endpoint_resolves_service_by_selected_vm_and_uses_existing_path(self):
        source=Path('app/client_portal.py').read_text(encoding='utf-8')
        self.assertIn("WHERE vm_id=? LIMIT 1',(vm_id,)",source)
        self.assertIn("self_actions.list(service['product_ref'], enabled_only=True)",source)
        self.assertIn('result=client_execute_self_action(action_id,payload,service)',source)
        self.assertIn("db.audit(service['service_ref'],'ADMIN_REMOTE_SELF_ACTION'",source)
        self.assertIn("dependencies=[Depends(authorized)],status_code=202",source)
        self.assertIn("is_due(parse_utc(service['expires_at']))",source)

    def test_remote_recent_records_include_self_outputs_with_no_separate_history_panel(self):
        js=Path('app/ui/admin.js').read_text(encoding='utf-8')
        html=Path('app/ui/index.html').read_text(encoding='utf-8')
        self.assertIn("renderOps(ops,self)",js)
        self.assertIn("item.type==='self-update'",js)
        self.assertIn("op.action==='COMMAND'&&op.state==='FAILED'",js)
        self.assertNotIn('id="remoteSelfHistory"',html)
        self.assertIn("request_id:generateRequestId(),confirm:true",js)

    def test_versioned_ui_resources(self):
        html=Path('app/ui/index.html').read_text(encoding='utf-8')
        version=Path('VERSION').read_text(encoding='utf-8').strip()
        self.assertIn('admin.js?v='+version,html)


if __name__=='__main__':
    unittest.main()
