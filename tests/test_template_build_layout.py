"""Golden template build stays under the VMO program and ships no VM disks."""
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import os
from app.config import Settings

ROOT=Path(__file__).resolve().parent.parent

class BuildLayoutTests(TestCase):
    def test_in_project_root_even_with_legacy_env(self):
        with patch.dict(os.environ, {'VMO_TEMPLATE_BUILD_ROOT':r'D:\VMware\Template-Build'}):
            settings=Settings.from_env()
        self.assertEqual(settings.template_build_root,ROOT/'template_build')

    def test_export_identity_free_oobe_and_v2(self):
        exporter=(ROOT/'template_build'/'Export-GuestToolkit.ps1').read_text(encoding='utf-8')
        prepare=(ROOT/'scripts'/'guest'/'Prepare-TemplateGuest.ps1').read_text(encoding='utf-8')
        seal=(ROOT/'scripts'/'guest'/'Seal-Win10Template.ps1').read_text(encoding='utf-8')
        self.assertIn('$DestinationRoot = $PSScriptRoot',exporter)
        self.assertNotIn("D:\\VMware\\Template-Build",exporter)
        self.assertIn('Prepare-TemplateGuest.ps1',exporter)
        self.assertNotIn('Inspect-Win10Template.ps1',exporter)
        self.assertIn('Install-OOBE-Bootstrap.ps1',prepare)
        self.assertIn('-TemplateMode',prepare)
        self.assertIn('agent.json',seal)
        self.assertIn('machine-binding.json',seal)
        self.assertIn('/generalize',seal)
        self.assertIn('/oobe',seal)
        self.assertIn('/shutdown',seal)
        self.assertNotIn('Inspect-Win10Template.ps1',seal)
        self.assertNotIn('LabOnlySkipDshIdentityCheck',seal)

    def test_release_excludes_build_artifacts(self):
        src=(ROOT/'tools'/'build_release.py').read_text(encoding='utf-8')
        self.assertIn("rel.parts[0] == 'template_build'",src)
        self.assertIn("TEMPLATE_TOOLS =",src)
        self.assertIn("len(rel.parts) != 2",src)

    def test_originals_stay_separate(self):
        source=(ROOT/'app'/'template_build.py').read_text(encoding='utf-8')
        self.assertIn('isolate_roots(root,templates_root,vms_root)',source)
        self.assertIn("self.store.add(record['name'],destination)",source)

    def test_host_updater_overlays_only_tools(self):
        updater=(ROOT/'scripts'/'host'/'Apply-GitHubUpdate.ps1').read_text(encoding='utf-8-sig')
        self.assertIn("$templateToolFiles = @('README.md','Export-GuestToolkit.ps1','00-Export-GuestToolkit.cmd')",updater)
        self.assertIn("Join-Path $Target 'template_build'",updater)
        self.assertIn("foreach ($tool in $templateToolFiles)",updater)
        folders=next(line for line in updater.splitlines() if line.startswith('$folders ='))
        self.assertNotIn("template_build",folders)
        self.assertNotIn("Remove-Item -LiteralPath $toolRoot",updater)

    def test_manual_installer_does_not_copy_build_disk(self):
        installer=(ROOT/'Install-All.ps1').read_text(encoding='utf-8-sig')
        self.assertIn("template_build\\README.md",installer)
        self.assertIn("template_build\\Export-GuestToolkit.ps1",installer)
        self.assertIn("template_build\\00-Export-GuestToolkit.cmd",installer)
        self.assertIn("if ($relative -match '^(?i:template_build)",installer)
        self.assertIn("if ($relative -notin",installer)
