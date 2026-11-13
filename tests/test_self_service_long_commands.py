"""Self-service preset commands share exact remote-admin limits and transport."""
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from app.remote_ops import (
    RemoteStore, MAX_COMMAND_CMD, MAX_COMMAND_POWERSHELL, MAX_COMMAND_BYTES,
)
from app.self_service import SelfServiceStore, ActionInput


class PresetCommandLengthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        database=Path(self.tmp.name)/'self.db'
        self.remote=RemoteStore(database)
        self.self_service=SelfServiceStore(database,remote_store=self.remote)

    def preset(self, shell, command):
        return ActionInput(product_ref='windows-basic',label='Large test preset',
                           kind='COMMAND',shell=shell,command=command,
                           run_mode='wait',enabled=True,timeout_seconds=60)

    def assert_remote_accepts_saved(self, stored):
        op=self.remote.queue('test-vm','COMMAND',{
            'shell':stored['shell'],'command':stored['command'],
            'privilege':'system','confirmed':True,'run_mode':stored['run_mode'],
            'cwd':stored['cwd'],'timeout_seconds':stored['timeout_seconds'],
        })
        self.assertTrue(op)

    def test_6000_char_powershell_roundtrips_and_is_dispatchable(self):
        command='$message = "'+('测'*5984)+'"\n$message'
        self.assertGreaterEqual(len(command),6000)
        stored=self.self_service.save(self.preset('powershell',command))
        self.assertEqual(self.self_service.get(stored['id'])['command'],command)
        self.assert_remote_accepts_saved(stored)

    def test_6000_char_cmd_roundtrips_and_is_dispatchable(self):
        command='echo '+('A'*5995)
        self.assertEqual(len(command),6000)
        stored=self.self_service.save(self.preset('cmd',command))
        self.assert_remote_accepts_saved(stored)

    def test_matches_shell_specific_remote_limits(self):
        for shell,limit in (('cmd',MAX_COMMAND_CMD),
                            ('powershell',MAX_COMMAND_POWERSHELL)):
            with self.subTest(shell=shell):
                stored=self.self_service.save(self.preset(shell,'A'*limit))
                self.assertEqual(len(stored['command']),limit)
                self.assert_remote_accepts_saved(stored)
                # CMD overage is rejected by SelfServiceStore; the absolute
                # PowerShell overage is rejected earlier by Pydantic.
                with self.assertRaises(ValueError):
                    self.self_service.save(self.preset(shell,'A'*(limit+1)))

    def test_pydantic_absolute_cap_matches_remote_powershell(self):
        with self.assertRaises(ValidationError):
            self.preset('powershell','A'*(MAX_COMMAND_POWERSHELL+1))

    def test_nul_and_transport_bytes_rejected_on_save(self):
        with self.assertRaisesRegex(ValueError,'预设命令无效'):
            self.self_service.save(self.preset('cmd','echo invalid\x00command'))
        # UTF-8 byte cap is still shared with RemoteStore. A command may be
        # below the character limit yet exceed the WSS JSON payload budget.
        huge='𠮷'*MAX_COMMAND_POWERSHELL
        self.assertLessEqual(len(huge),MAX_COMMAND_POWERSHELL)
        self.assertLess(len(huge.encode('utf-8')),MAX_COMMAND_BYTES)
        self.assertEqual(MAX_COMMAND_BYTES,64*1024)

    def test_existing_2k_presets_can_be_edited_to_6k(self):
        first=self.self_service.save(self.preset('powershell','A'*2000))
        updated=self.preset('powershell','B'*6000)
        updated.id=first['id']
        revised=self.self_service.save(updated)
        self.assertEqual(revised['id'],first['id'])
        self.assertEqual(revised['command'],'B'*6000)

    def test_ui_has_dynamic_limits_and_counter(self):
        root=Path(__file__).resolve().parents[1]
        html=(root/'app/ui/index.html').read_text(encoding='utf-8')
        js=(root/'app/ui/admin.js').read_text(encoding='utf-8')
        self.assertIn('id="selfCommand" rows="8" maxlength="12000"',html)
        self.assertIn('id="selfCommandCount"',html)
        self.assertIn("const commandLimit=()=>$('selfShell').value==='cmd'?8000:12000;",js)
        self.assertIn("$('selfShell').onchange=commandCount;",js)
        self.assertIn("$('selfCommand').oninput=commandCount;",js)
        self.assertIn("body.command.length>commandLimit()",js)


if __name__=='__main__':
    unittest.main()
