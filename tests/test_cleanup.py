"""Safety regressions for program cleanup and registered reinstall backups."""
from __future__ import annotations
from contextlib import contextmanager
import os
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


if __name__ == '__main__':
    unittest.main()
