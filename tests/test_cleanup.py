"""Safety regressions for program cleanup and registered reinstall backups."""
from __future__ import annotations
from contextlib import contextmanager
import os
import json
import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
import uuid

from app.cleanup import CleanupError, CleanupManager
from app.fleet import FleetStore


class FakeStore:
    def __init__(self, db, vm):
        self.db_path, self.vm = db, vm

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            yield conn
        finally:
            conn.close()

    def get_vm(self, vm_id):
        return self.vm if vm_id == self.vm['id'] else None


class FakeVMRun:
    def is_running(self, vmx):
        return False


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / 'VM-Orchestrator'
        self.vms = base / 'Customers'
        self.root.mkdir()
        self.vms.mkdir()
        self.db = self.root / 'data' / 'templates.sqlite3'
        self.fleet = FleetStore(self.db)
        with self.fleet.db() as d:
            d.execute("CREATE TABLE jobs (status TEXT NOT NULL)")
        self.name = 'CustomerA'
        self.job = str(uuid.uuid4())
        self.vm_id = str(uuid.uuid4())
        vm_dir = self.vms / self.name
        vm_dir.mkdir()
        self.vmx = vm_dir / (self.name + '.vmx')
        self.vmx.write_text('config.version = "8"\n')
        self.oldroot = self.vms / '.vmo-reinstall-backups'
        self.oldroot.mkdir()
        self.old = self.oldroot / (self.name + '-' + self.job)
        self.old.mkdir()
        (self.old / (self.name + '.vmx')).write_text('config.version = "8"\n')
        (self.old / 'previous.vmdk').write_bytes(b'old-disk-descriptor')
        with self.fleet.db() as d:
            d.execute("INSERT INTO vm_reinstalls "
                      "(job_id, vm_id, old_template_id, new_template_id, backup_path, "
                      "status, created_at, completed_at) VALUES (?,?,?,?,?,?,?,?)",
                      (self.job, self.vm_id, 'old', 'new', str(self.old), 'SUCCEEDED',
                       '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'))
        self.vm = dict(id=self.vm_id, name=self.name, state='READY',
                       vmx_path=str(self.vmx))
        self.store = FakeStore(self.db, self.vm)
        self.manager = CleanupManager(self.root, self.vms, self.db,
                                      self.store, self.fleet, FakeVMRun())
        self.updates = self.root / 'data' / 'updates'
        self.updates.mkdir()
        self.cache = self.updates / 'stage-v1.0.0'
        self.cache.mkdir()
        (self.cache / 'temp.zip').write_bytes(b'temp')
        old_time = time.time() - 10 * 86400
        os.utime(self.cache, (old_time, old_time))

    def test_scan_only_known_successful_reinstall_and_old_cache(self):
        found = self.manager.scan()
        ids = {i['id'] for i in found['items']}
        self.assertIn('reinstall:' + self.job, ids)
        self.assertIn('stage:stage-v1.0.0', ids)
        old = next(x for x in found['items'] if x['id'] == 'reinstall:' + self.job)
        self.assertTrue(old['manual'])
        self.assertGreater(old['bytes'], 0)

    def test_explicit_backup_confirmation_and_history_retention(self):
        item = next(x for x in self.manager.scan()['items']
                    if x['kind'] == 'reinstall_backup')
        selected = [{'id': item['id'], 'fingerprint': item['fingerprint']}]
        with self.assertRaises(CleanupError):
            self.manager.delete(selected, confirm_backups=False)
        result = self.manager.delete(selected, confirm_backups=True)
        self.assertEqual(len(result['deleted']), 1)
        self.assertFalse(self.old.exists())
        self.assertTrue(self.vmx.exists())
        with self.fleet.db() as d:
            row = d.execute('SELECT backup_deleted_at,backup_path FROM vm_reinstalls '
                            'WHERE job_id=?', (self.job,)).fetchone()
            self.assertIsNotNone(row['backup_deleted_at'])
            self.assertEqual(row['backup_path'], str(self.old))

    def test_stale_fingerprint_rejected(self):
        item = next(x for x in self.manager.scan()['items'] if x['kind'] == 'program_cache')
        (self.cache / 'temp.zip').write_bytes(b'changed')
        with self.assertRaises(CleanupError):
            self.manager.delete([{'id': item['id'], 'fingerprint': item['fingerprint']}])
        self.assertTrue(self.cache.exists())

    def test_protect_busy_vm_task(self):
        with self.fleet.db() as d:
            d.execute("INSERT INTO jobs VALUES('RUNNING')")
        with self.assertRaises(CleanupError):
            self.manager.scan()

    def test_reject_symlink_in_backup(self):
        try:
            (self.old / 'escape-link').symlink_to(self.vmx)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks are not supported on this runner')
        found = self.manager.scan()
        self.assertFalse(any(x['kind'] == 'reinstall_backup' for x in found['items']))
        self.assertTrue(found['protected'])
        self.assertTrue(self.old.exists())


    def test_recent_staging_and_generated_updater_files_are_detected(self):
        (self.root / 'VERSION').write_text('1.4.51\n', encoding='utf-8')
        newer = self.updates / 'unpacked-v1.4.50'
        newer.mkdir()
        (newer / 'file.py').write_text('old unpacked code')
        old_script = self.updates / 'Apply-GitHubUpdate-v1.4.44.ps1'
        old_script.write_text('old')
        active_script = self.updates / 'Apply-GitHubUpdate-v1.4.51.ps1'
        active_script.write_text('keep')
        found = self.manager.scan()
        ids = {x['id'] for x in found['items']}
        self.assertIn('stage:unpacked-v1.4.50', ids)
        self.assertIn('artifact:Apply-GitHubUpdate-v1.4.44.ps1', ids)
        self.assertNotIn('artifact:Apply-GitHubUpdate-v1.4.51.ps1', ids)
        self.assertTrue(any(x['path'] == str(active_script) for x in found['protected']))

    def test_release_manifest_protects_modified_code_and_marks_extras_manual(self):
        (self.root / 'VERSION').write_text('1.4.51\n', encoding='utf-8')
        app = self.root / 'app'
        app.mkdir()
        original = app / 'main.py'
        original.write_bytes(b'shipped')
        extra = app / 'former.py'
        extra.write_bytes(b'old script')
        (self.root / 'release-manifest.json').write_text(json.dumps({
            'version': '1.4.51',
            'files': {'app/main.py': {'size': 7,
                        'sha256': hashlib.sha256(b'shipped').hexdigest()}}
        }))
        found = self.manager.scan()
        candidates = [x for x in found['items'] if x['kind'] == 'program_extra']
        self.assertEqual([x['path'] for x in candidates], [str(extra)])
        self.assertTrue(candidates[0]['manual'])
        self.assertNotIn(str(original), [x['path'] for x in found['protected']])
        original.write_bytes(b'patched')
        self.assertTrue(any(x['path'] == str(original)
                            for x in self.manager.scan()['protected']))
        item = next(x for x in self.manager.scan()['items']
                    if x['kind'] == 'program_extra')
        self.manager.delete([{'id': item['id'], 'fingerprint': item['fingerprint']}])
        self.assertFalse(extra.exists())
        self.assertTrue(original.exists())

    def test_keep_newest_two_rollback_archives_even_when_all_are_recent(self):
        back = self.updates / 'backups'
        back.mkdir()
        for version in ('1.4.46', '1.4.47', '1.4.48'):
            path = back / ('before-v' + version + '-20261111-230000')
            path.mkdir()
            (path / 'code').mkdir()
            (path / 'code' / 'VERSION').write_text(version)
            os.utime(path, (time.time(), time.time()))
            time.sleep(.01)
        found = self.manager.scan()
        old = [x for x in found['items'] if x['kind'] == 'code_backup']
        self.assertEqual(len(old), 1)
        self.assertTrue(old[0]['manual'])
        self.assertGreaterEqual(sum('最近两份' in x['reason'] for x in found['protected']), 2)


if __name__ == '__main__':
    unittest.main()
