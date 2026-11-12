"""Regression guards for host restart-only controls and tenant-safe command results."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from app.client_output import customer_command_result, safe_excerpt
from app.host_control import HostControl, HostControlError, _tail


class ClientResultTests(unittest.TestCase):
    def test_failure_shows_real_chinese_stderr_not_generic_exit_code(self):
        result = customer_command_result(
            {'exit_code': 1, 'stdout': '',
             'stderr': '文件已经被另一程序使用时，进程无法访问此文件。'},
            'FAILED')
        self.assertIn('文件已经被另一程序使用', result['output_text'])
        self.assertNotIn('命令退出码 1', result['output_text'])

    def test_stdout_used_if_stderr_missing(self):
        data = customer_command_result({'exit_code': 1, 'stdout': '目标已存在',
                                        'stderr': ''}, 'FAILED')
        self.assertEqual(data['output_text'], '目标已存在')
        data = customer_command_result({'exit_code': 0, 'stdout': '安装完成'}, 'SUCCEEDED')
        self.assertEqual(data['output_text'], '安装完成')

    def test_redaction_bounded_and_no_full_result(self):
        data = customer_command_result({
            'exit_code': 1, 'stderr': 'Access denied\npassword=SuperSecret\nBearer abc.def\n' + ('x' * 1800),
            'command': 'net user admin SecretPassword',
            'cwd': 'C:\\private\\'
        }, 'FAILED')
        output = data['output_text']
        self.assertIn('Access denied', output)
        self.assertNotIn('SuperSecret', output)
        self.assertNotIn('abc.def', output)
        self.assertNotIn('net user', json.dumps(data))
        self.assertLessEqual(len(output), 801)

    def test_interactive_is_not_misreported_as_app_health(self):
        data = customer_command_result({'launched': True, 'pid': 3456}, 'SUCCEEDED')
        self.assertIn('仍需检查', data['output_text'])


class HostControlTests(unittest.TestCase):
    def test_log_read_is_bounded_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'host-console.log'
            path.write_text('Boot OK\npassword=abc123\nBearer secret.token\n', encoding='utf-8')
            data = _tail(path)
            self.assertIn('Boot OK', data)
            self.assertNotIn('abc123', data)
            self.assertNotIn('secret.token', data)

    def test_status_only_exposes_whitelisted_fields_and_safe_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / 'data' / 'updates'
            work.mkdir(parents=True)
            status_file = work / 'host-control-status.json'
            status_file.write_text(json.dumps({
                'state': 'failed', 'attempt': 'b' * 32,
                'action': 'restart', 'message': 'Expected error',
                'secret': 'NEVER_SHOW',
            }), encoding='utf-8')
            host = HostControl(root)
            report = host.status()
            self.assertTrue(report['running'])
            self.assertEqual(report['restart']['state'], 'failed')
            self.assertNotIn('secret', report['restart'])
            self.assertNotIn('NEVER_SHOW', str(report))
            self.assertEqual(host.logs()['logs']['update_stdout'], '')

    @unittest.skipIf(os.name == 'nt', 'Windows restart launches a real process: never invoke in unit tests')
    def test_reject_non_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HostControlError):
                HostControl(Path(directory)).restart()


class UIContractsTests(unittest.TestCase):
    def test_no_stop_management_button_or_endpoint(self):
        js = Path('app/ui/admin.js').read_text(encoding='utf-8')
        html = Path('app/ui/index.html').read_text(encoding='utf-8')
        main = Path('app/main.py').read_text(encoding='utf-8')
        script = Path('scripts/host/Control-VMOHost.ps1').read_text(encoding='utf-8')
        self.assertIn('hostRestartBtn', html)
        self.assertNotIn('hostStopBtn', html)
        self.assertNotIn('/api/system/host/stop', main)
        self.assertNotIn("ValidateSet('Restart','Stop')", script)
        self.assertNotIn("if($Action -eq 'Stop')", script)
        self.assertIn("'/api/system/host/restart'", js)
        self.assertIn('verify_session(request)', main)

    def test_system_update_page_survives_reload(self):
        js = Path('app/ui/admin.js').read_text(encoding='utf-8')
        html = Path('app/ui/index.html').read_text(encoding='utf-8')
        self.assertIn("url.searchParams.set('page',page)", js)
        self.assertIn("new URLSearchParams(window.location.search).get('page')", js)
        self.assertNotIn("window.location.reload();return;", js[js.index('function show(data)'):js.index('window.VMOSystemUpdate=')])
        expected = Path('VERSION').read_text(encoding='utf-8').strip()
        self.assertIn('v='+expected, html)

    def test_customer_history_displays_bounded_text_not_generic_code(self):
        portal = Path('app/client_portal.py').read_text(encoding='utf-8')
        js = Path('app/ui/client.js').read_text(encoding='utf-8')
        self.assertIn('customer_command_result', portal)
        self.assertIn('a.output_text', js)
        self.assertNotIn("命令退出码 '+str(result['exit_code'])", portal)


if __name__ == '__main__':
    unittest.main()
