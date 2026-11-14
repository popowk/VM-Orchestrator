"""Missing reinstall backups are only acknowledged in the DB, never deleted."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
import uuid

from app.cleanup import CleanupManager,CleanupError
from app.fleet import FleetStore


class VMStore:
    def __init__(self,db,vm):
        self.db=db
        self.vm=vm
    @contextmanager
    def _conn(self):
        with sqlite3.connect(self.db) as connection:
            yield connection
    def get_vm(self,vm_id):
        return self.vm if vm_id==self.vm['id'] else None


class VMRun:
    def is_running(self,vmx):
        return False


class MissingBackupTests(unittest.TestCase):
    def setUp(self):
        t=TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root=Path(t.name)/'VM-Orchestrator'
        self.root.mkdir()
        self.vms=Path(t.name)/'Customers'
        self.vms.mkdir()
        self.db=self.root/'db.sqlite3'
        self.fleet=FleetStore(self.db)
        with self.fleet.db() as d:
            d.execute('CREATE TABLE jobs(status TEXT)')
        self.vm_id=str(uuid.uuid4())
        self.job=str(uuid.uuid4())
        self.current=self.vms/'001'
        self.current.mkdir()
        (self.current/'001.vmx').write_text('config.version = "8"\n')
        self.backups=self.vms/'.vmo-reinstall-backups'
        self.backups.mkdir()
        self.missing=self.backups/('001-'+self.job)
        with self.fleet.db() as d:
            d.execute("""INSERT INTO vm_reinstalls
                    (job_id,vm_id,old_template_id,new_template_id,
                     backup_path,status,created_at,completed_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (self.job,self.vm_id,'t1','t2',str(self.missing),'SUCCEEDED',
                     '2026-01-01','2026-01-02'))
        self.vm={'id':self.vm_id,'name':'001','state':'READY',
                 'vmx_path':str(self.current/'001.vmx')}
        self.manager=CleanupManager(self.root,self.vms,self.db,
                                    VMStore(self.db,self.vm),self.fleet,VMRun())

    def test_missing_directory_is_separate_non_deletable_record(self):
        result=self.manager.scan()
        self.assertEqual(result['missing_count'],1)
        self.assertEqual(result['missing_records'][0]['job_id'],self.job)
        self.assertEqual(result['missing_records'][0]['bytes'],0)
        self.assertFalse(any(row['kind']=='reinstall_backup' for row in result['items']))
        self.assertNotIn('Backup path does not match',
                         str(result['protected']))
        self.assertTrue((self.current/'001.vmx').exists())

    def test_reconcile_missing_updates_only_marker_and_retains_audit(self):
        result=self.manager.acknowledge_missing([self.job])
        self.assertEqual(result['files_deleted'],0)
        self.assertEqual(result['bytes_released'],0)
        with self.fleet.db() as d:
            row=d.execute('SELECT * FROM vm_reinstalls WHERE job_id=?',(self.job,)).fetchone()
        self.assertEqual(row['status'],'SUCCEEDED')
        self.assertIsNone(row['backup_deleted_at'])
        self.assertIsNotNone(row['backup_missing_at'])
        self.assertEqual(row['backup_path'],str(self.missing))
        self.assertTrue((self.current/'001.vmx').exists())
        self.assertFalse(self.missing.exists())
        self.assertEqual(self.manager.scan()['missing_count'],0)

    def test_reappearing_folder_is_never_acknowledged(self):
        self.missing.mkdir()
        with self.assertRaises(CleanupError):
            self.manager.acknowledge_missing([self.job])
        with self.fleet.db() as d:
            row=d.execute('SELECT backup_missing_at FROM vm_reinstalls WHERE job_id=?',
                          (self.job,)).fetchone()
        self.assertIsNone(row['backup_missing_at'])
        self.assertTrue(self.missing.is_dir())

    def test_untracked_backup_folder_is_protected_not_deleted(self):
        orphan=self.backups/('001-'+str(uuid.uuid4()))
        orphan.mkdir()
        (orphan/'data.vmdk').write_bytes(b'secret')
        scan=self.manager.scan()
        self.assertTrue(any(str(orphan)==row['path'] for row in scan['protected']))
        self.assertFalse(any(row['path']==str(orphan) for row in scan['items']))
        self.assertTrue((orphan/'data.vmdk').exists())

    def test_mismatched_record_is_not_auto_acked(self):
        with self.fleet.db() as d:
            d.execute('UPDATE vm_reinstalls SET backup_path=? WHERE job_id=?',
                      (str(self.backups/'unrelated'),self.job))
        scan=self.manager.scan()
        self.assertEqual(scan['missing_count'],0)
        self.assertTrue(any('不匹配' in x['reason'] for x in scan['protected']))
        with self.assertRaises(CleanupError):
            self.manager.acknowledge_missing([self.job])


if __name__=='__main__':
    unittest.main()
