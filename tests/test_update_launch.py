"""Updater release gates: transparent Windows PowerShell invocation and ACK."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class HostUpdaterLaunchTests(unittest.TestCase):
    def test_no_encoded_powershell_payload(self):
        code = (ROOT / 'app' / 'system_updates.py').read_text(encoding='utf-8')
        self.assertNotIn('-EncodedCommand', code)
        self.assertNotIn('encoded_bootstrap', code)
        self.assertIn('"RemoteSigned", "-File", str(preflight)', code)
        self.assertIn('"RemoteSigned", "-File", str(runner)', code)
        self.assertIn('getattr(exc, "winerror", None)', code)

    def test_preflight_file_is_read_only_and_updater_writes_ack(self):
        test_script = (ROOT / 'scripts' / 'host' / 'Test-GitHubUpdate.ps1').read_text(encoding='utf-8')
        updater = (ROOT / 'scripts' / 'host' / 'Apply-GitHubUpdate.ps1').read_text(encoding='utf-8')
        self.assertIn('Parser]::ParseFile', test_script)
        self.assertNotIn('Stop-Process', test_script)
        self.assertIn("'updater-ack-' + $Attempt + '.txt'", updater)
        self.assertIn("[IO.File]::WriteAllText($ackFile", updater)
        self.assertLess(updater.index('[IO.File]::WriteAllText($ackFile'), updater.index('function Log('))


if __name__ == '__main__':
    unittest.main()
