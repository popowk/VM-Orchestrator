"""Regression: an old updater must not leave template_build without tools."""
import tempfile
import unittest
from pathlib import Path

from app.template_build import restore_missing_template_entrypoints

ROOT = Path(__file__).resolve().parent.parent


class OneClickGoldenTemplateTests(unittest.TestCase):
    def test_old_updater_recovers_missing_host_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            build = Path(temp) / "template_build"
            build.mkdir()
            disk = build / "ExistingBUILD" / "System.vmdk"
            disk.parent.mkdir()
            disk.write_bytes(b"customer fixture must not be removed")
            restore_missing_template_entrypoints(build)
            cmd = build / "00-Export-GuestToolkit.cmd"
            ps1 = build / "Export-GuestToolkit.ps1"
            self.assertTrue(cmd.is_file())
            self.assertTrue(ps1.is_file())
            self.assertIn("scripts\\\\host\\\\Export-GuestToolkit.ps1", ps1.read_text())
            ps1.write_text("manual custom launcher", encoding="utf-8")
            restore_missing_template_entrypoints(build)
            self.assertEqual(ps1.read_text(encoding="utf-8"), "manual custom launcher")
            self.assertEqual(disk.read_bytes(), b"customer fixture must not be removed")

    def test_source_and_package_contain_host_fallback(self):
        exporter = (ROOT/"scripts"/"host"/"Export-GuestToolkit.ps1").read_text(encoding="utf-8")
        self.assertIn("01-Prepare-And-Verify.cmd", exporter)
        self.assertIn("02-Verify-And-Seal.cmd", exporter)
        self.assertIn("03-Check-Status.cmd", exporter)
        self.assertIn("Join-Path $dest 'Core'", exporter)
        self.assertIn("Join-Path $dest 'Agent'", exporter)
        self.assertIn("if (Test-Path -LiteralPath $dest)", exporter)
        self.assertIn("Read-Host", (ROOT/"scripts"/"guest"/"Run-TemplateStep.ps1").read_text(encoding="utf-8"))
        self.assertIn("Check-TemplateStatus.ps1", exporter)
        self.assertTrue((ROOT/"scripts"/"host"/"00-Export-GuestToolkit.cmd").is_file())
        self.assertTrue((ROOT/"template_build"/"00-Export-GuestToolkit.cmd").is_file())
        release = (ROOT/"tools"/"build_release.py").read_text(encoding="utf-8")
        self.assertIn("'00-Export-GuestToolkit.cmd'", release)

    def test_legacy_update_script_guarantees_copy_of_host_exporter(self):
        updater = (ROOT/"scripts"/"host"/"Apply-GitHubUpdate.ps1").read_text(encoding="utf-8")
        self.assertIn("$folders = @('app','guest_agent','scripts'", updater)
        self.assertIn("00-Export-GuestToolkit.cmd", updater)
        self.assertIn("foreach ($tool in $templateToolFiles)", updater)
        self.assertNotIn("Remove-Item -LiteralPath $toolRoot", updater)
        installer = (ROOT/"Install-All.ps1").read_text(encoding="utf-8")
        self.assertIn("template_build\\00-Export-GuestToolkit.cmd", installer)


if __name__ == "__main__":
    unittest.main()
