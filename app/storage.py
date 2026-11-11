from __future__ import annotations
"""SQLite persistence: immutable templates, managed VMs and restart-safe job states."""
import sqlite3
from contextlib import contextmanager
import uuid
from datetime import datetime, timezone
from pathlib import Path


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class StoreConflict(ValueError):
    pass


class TemplateStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS templates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                vmx_path TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'CANDIDATE',
                attested_by TEXT, sealed_at TEXT, created_at TEXT NOT NULL,
                oobe_bootstrap INTEGER NOT NULL DEFAULT 0
            )''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(templates)')}
            if 'oobe_bootstrap' not in columns:
                db.execute('ALTER TABLE templates ADD COLUMN oobe_bootstrap INTEGER NOT NULL DEFAULT 0')
            if 'files_deleted_at' not in columns:
                db.execute('ALTER TABLE templates ADD COLUMN files_deleted_at TEXT')
            if 'oobe_bootstrap_version' not in columns:
                db.execute('ALTER TABLE templates ADD COLUMN oobe_bootstrap_version INTEGER NOT NULL DEFAULT 0')
                # V1.1 already-sealed templates have the old VMOSetup bootstrap.
                db.execute('UPDATE templates SET oobe_bootstrap_version=1 WHERE oobe_bootstrap=1')
            db.execute('''CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY, template_id TEXT NOT NULL,
                action TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS vms (
                id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                vmx_path TEXT NOT NULL UNIQUE, template_id TEXT NOT NULL,
                customer_ref TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(template_id) REFERENCES templates(id)
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, vm_id TEXT NOT NULL, action TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL,
                started_at TEXT, completed_at TEXT,
                FOREIGN KEY(vm_id) REFERENCES vms(id)
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS vm_deletions (
                vm_id TEXT PRIMARY KEY, original_name TEXT NOT NULL,
                original_vmx_path TEXT NOT NULL, deleted_at TEXT NOT NULL
            )''')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_job_per_vm ON jobs(vm_id) "
                       "WHERE status IN ('QUEUED','RUNNING')")

    @contextmanager
    def _conn(self):
        db = sqlite3.connect(str(self.db_path), timeout=20)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=20000')
            db.execute('PRAGMA foreign_keys=ON')
            with db:
                yield db
        finally:
            db.close()

    def list(self) -> list[dict]:
        with self._conn() as db:
            return [dict(r) for r in db.execute("SELECT * FROM templates WHERE status!='INTERNAL_REMOVED' ORDER BY created_at DESC")]

    def get(self, template_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute('SELECT * FROM templates WHERE id=?', (template_id,)).fetchone()
            return dict(row) if row else None

    def add(self, name: str, vmx_path: Path) -> dict:
        id_ = str(uuid.uuid4())
        with self._conn() as db:
            db.execute('INSERT INTO templates (id, name, vmx_path, created_at) VALUES (?, ?, ?, ?)',
                       (id_, name, str(vmx_path), timestamp()))
            db.execute('INSERT INTO audit_events VALUES (?, ?, ?, ?, ?)',
                       (str(uuid.uuid4()), id_, 'REGISTER', 'Template candidate added', timestamp()))
        return self.get(id_)

    def template_reference_count(self, template_id: str) -> int:
        # Includes previously deleted VMs retained for audit and reinstalls.
        with self._conn() as db:
            return db.execute('SELECT count(*) FROM vms WHERE template_id=?', (template_id,)).fetchone()[0]

    def archive_template(self, template_id: str) -> dict:
        with self._conn() as db:
            cur = db.execute("UPDATE templates SET status='ARCHIVED' WHERE id=? AND status IN ('CANDIDATE','SEALED_ATTESTED')", (template_id,))
            if cur.rowcount != 1:
                raise StoreConflict('Template missing or already archived')
            db.execute('INSERT INTO audit_events VALUES (?,?,?,?,?)',
                       (str(uuid.uuid4()), template_id, 'ARCHIVED', 'Unlisted without deleting disk files', timestamp()))
        return self.get(template_id)

    def restore_template(self, template_id: str) -> dict:
        with self._conn() as db:
            cur = db.execute("UPDATE templates SET status='CANDIDATE',attested_by=NULL,sealed_at=NULL,oobe_bootstrap=0,oobe_bootstrap_version=0 WHERE id=? AND status='ARCHIVED'", (template_id,))
            if cur.rowcount != 1: raise StoreConflict('Only archived templates can be restored')
            db.execute('INSERT INTO audit_events VALUES (?,?,?,?,?)',
                       (str(uuid.uuid4()), template_id, 'UNARCHIVE', 'Re-register as candidate; resealing required', timestamp()))
        return self.get(template_id)

    def remove_template_record(self, template_id: str) -> None:
        with self._conn() as db:
            if db.execute('SELECT count(*) FROM vms WHERE template_id=?',(template_id,)).fetchone()[0]:
                raise StoreConflict('Template has associated VM records')
            cur = db.execute('DELETE FROM templates WHERE id=?',(template_id,))
            if cur.rowcount != 1:
                raise StoreConflict('Template missing during removal')
            db.execute('INSERT INTO audit_events VALUES (?,?,?,?,?)',
                       (str(uuid.uuid4()), template_id, 'PHYSICAL_DELETE', 'Dedicated template folder removed', timestamp()))

    def begin_archived_template_purge(self, template_id: str) -> dict:
        """Reserve an archived, immutable template for disk-only removal.

        Historical VM rows and audit events MUST remain. FILES_PURGING is a
        recoverable failure state when filesystem deletion is interrupted.
        """
        with self._conn() as db:
            cur = db.execute("UPDATE templates SET status='FILES_PURGING' WHERE id=? AND status='ARCHIVED' AND files_deleted_at IS NULL", (template_id,))
            if cur.rowcount != 1:
                raise StoreConflict('Only archived templates with intact files can be purged')
            db.execute('INSERT INTO audit_events VALUES (?,?,?,?,?)',
                       (str(uuid.uuid4()), template_id, 'DISK_PURGE_STARTED', 'Archived template disk cleanup started; historical references retained', timestamp()))
        return self.get(template_id)

    def complete_archived_template_purge(self, template_id: str) -> dict:
        with self._conn() as db:
            now = timestamp()
            cur = db.execute("UPDATE templates SET status='FILES_DELETED', files_deleted_at=? WHERE id=? AND status='FILES_PURGING'", (now, template_id))
            if cur.rowcount != 1:
                raise StoreConflict('Template disk purge was not in progress')
            db.execute('INSERT INTO audit_events VALUES (?,?,?,?,?)',
                       (str(uuid.uuid4()), template_id, 'DISK_PURGED', 'Template disk files removed; VM/template provenance retained', now))
        return self.get(template_id)

    def revert_archived_template_purge(self, template_id: str) -> dict:
        with self._conn() as db:
            cur = db.execute("UPDATE templates SET status='ARCHIVED' WHERE id=? AND status='FILES_PURGING'", (template_id,))
            if cur.rowcount != 1: raise StoreConflict('Not a pending template purge')
            db.execute('INSERT INTO audit_events VALUES (?,?,?,?,?)',
                       (str(uuid.uuid4()), template_id, 'DISK_PURGE_ROLLBACK', 'File operation failed and original directory was restored', timestamp()))
        return self.get(template_id)

    def forget_template_history(self, template_id: str) -> dict:
        """Irreversibly discard a template record and provenance while keeping VM rows.

        SQLite requires every vms.template_id foreign key to have a parent. All
        references are therefore rewritten to a shared, invisible tombstone.
        This never deletes VM folders, jobs, snapshots, or installed software.
        """
        retired = '__VMO_SOURCE_FORGOTTEN__'
        with self._conn() as db:
            t=db.execute('SELECT status FROM templates WHERE id=?', (template_id,)).fetchone()
            if not t or t['status'] not in ('FILES_DELETED','ARCHIVED'):
                raise StoreConflict('Only archived templates with missing VMX, or already purged templates, can be forgotten')
            db.execute("INSERT OR IGNORE INTO templates (id,name,vmx_path,status,created_at) VALUES (?,?,?,?,?)",
                       (retired,'(source history removed)','__VMO_INTERNAL_RETIRED_SOURCE__',
                        'INTERNAL_REMOVED',timestamp()))
            changed=db.execute('UPDATE vms SET template_id=? WHERE template_id=?',
                               (retired,template_id)).rowcount
            # These are source history fields, not live Guest Agent credentials.
            if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vm_reinstalls'").fetchone():
                db.execute('UPDATE vm_reinstalls SET old_template_id=? WHERE old_template_id=?', (retired,template_id))
                db.execute('UPDATE vm_reinstalls SET new_template_id=? WHERE new_template_id=?', (retired,template_id))
            history=db.execute('DELETE FROM audit_events WHERE template_id=?',(template_id,)).rowcount
            result=db.execute('DELETE FROM templates WHERE id=?',(template_id,))
            if result.rowcount != 1: raise StoreConflict('Template record deletion failed')
            return {'forgotten':True,'template_id':template_id,'vm_records_preserved':changed,
                    'audit_rows_deleted':history,'original_source_metadata_deleted':True}

    def seal_attested(self, template_id: str, operator: str, oobe_bootstrap: bool = False,
                      bootstrap_version: int = 0) -> dict:
        with self._conn() as db:
            cur = db.execute("UPDATE templates SET status='SEALED_ATTESTED', attested_by=?, sealed_at=?, oobe_bootstrap=?, oobe_bootstrap_version=? "
                             "WHERE id=? AND status='CANDIDATE'", (operator, timestamp(), int(oobe_bootstrap), bootstrap_version, template_id))
            if cur.rowcount != 1:
                raise ValueError('Only CANDIDATE templates can be sealed')
            db.execute('INSERT INTO audit_events VALUES (?, ?, ?, ?, ?)',
                       (str(uuid.uuid4()), template_id, 'SEAL_ATTESTED', operator, timestamp()))
        return self.get(template_id)

    def list_vms(self) -> list[dict]:
        with self._conn() as db:
            return [dict(r) for r in db.execute("SELECT * FROM vms WHERE state != 'DELETED' ORDER BY created_at DESC")]

    def get_vm(self, vm_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute('SELECT * FROM vms WHERE id=?', (vm_id,)).fetchone()
            return dict(row) if row else None

    def get_job(self, job_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute('''SELECT j.*, COALESCE(d.original_name,v.name) AS vm_name
                                FROM jobs j JOIN vms v ON v.id=j.vm_id
                                LEFT JOIN vm_deletions d ON d.vm_id=j.vm_id
                                WHERE j.id=?''', (job_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self, vm_id: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        limit = max(1, min(int(limit), 100)); offset = max(0, int(offset))
        with self._conn() as db:
            if vm_id:
                rows = db.execute('''SELECT j.*, COALESCE(d.original_name,v.name) AS vm_name
                                     FROM jobs j JOIN vms v ON v.id=j.vm_id
                                     LEFT JOIN vm_deletions d ON d.vm_id=j.vm_id
                                     WHERE j.vm_id=? ORDER BY j.created_at DESC, j.id DESC LIMIT ? OFFSET ?''', (vm_id, limit, offset))
            else:
                rows = db.execute('''SELECT j.*, COALESCE(d.original_name,v.name) AS vm_name
                                     FROM jobs j JOIN vms v ON v.id=j.vm_id
                                     LEFT JOIN vm_deletions d ON d.vm_id=j.vm_id
                                     ORDER BY j.created_at DESC, j.id DESC LIMIT ? OFFSET ?''', (limit, offset))
            return [dict(r) for r in rows]

    def count_jobs(self, vm_id: str | None = None) -> int:
        with self._conn() as db:
            if vm_id:
                row = db.execute('SELECT COUNT(*) FROM jobs WHERE vm_id=?', (vm_id,)).fetchone()
            else:
                row = db.execute('SELECT COUNT(*) FROM jobs').fetchone()
            return int(row[0])

    def _new_job(self, db, vm_id: str, action: str, detail: str = '') -> str:
        job_id = str(uuid.uuid4())
        try:
            db.execute('INSERT INTO jobs(id,vm_id,action,status,detail,created_at) VALUES (?,?,?,?,?,?)',
                       (job_id, vm_id, action, 'QUEUED', detail, timestamp()))
        except sqlite3.IntegrityError as exc:
            raise StoreConflict('VM already has an active job') from exc
        return job_id

    def queue_clone(self, template_id: str, name: str, dst: Path, customer_ref: str | None):
        vm_id = str(uuid.uuid4())
        with self._conn() as db:
            template = db.execute('SELECT status FROM templates WHERE id=?', (template_id,)).fetchone()
            if not template or template['status'] != 'SEALED_ATTESTED':
                raise StoreConflict('Template must be registered and SEALED_ATTESTED')
            try:
                db.execute('''INSERT INTO vms(id,name,vmx_path,template_id,customer_ref,state,created_at)
                              VALUES (?,?,?,?,?,?,?)''',
                           (vm_id, name, str(dst), template_id, customer_ref, 'CREATING', timestamp()))
            except sqlite3.IntegrityError as exc:
                raise StoreConflict('VM name or destination already reserved') from exc
            job_id = self._new_job(db, vm_id, 'CLONE_FULL')
        return self.get_vm(vm_id), self.get_job(job_id)

    def queue_action(self, vm_id: str, action: str, detail: str = '') -> dict:
        with self._conn() as db:
            vm = db.execute('SELECT state FROM vms WHERE id=?', (vm_id,)).fetchone()
            if not vm:
                raise StoreConflict('VM does not exist')
            if vm['state'] != 'READY':
                raise StoreConflict('VM is not ready')
            job_id = self._new_job(db, vm_id, action, detail)
        return self.get_job(job_id)

    def queue_reinstall(self, vm_id: str, template_id: str) -> dict:
        """Reserve exactly one destructive re-image job; retain VM identity and label."""
        with self._conn() as db:
            vm = db.execute('SELECT state FROM vms WHERE id=?', (vm_id,)).fetchone()
            template = db.execute('SELECT status FROM templates WHERE id=?', (template_id,)).fetchone()
            if not vm or vm['state'] != 'READY':
                raise StoreConflict('Only READY VMs can be reinstalled')
            if not template or template['status'] != 'SEALED_ATTESTED':
                raise StoreConflict('Reinstall source must be a sealed golden template')
            job_id = self._new_job(db, vm_id, 'REINSTALL_FROM_TEMPLATE')
            db.execute("UPDATE vms SET state='REINSTALLING' WHERE id=?", (vm_id,))
        return self.get_job(job_id)

    def finish_reinstall(self, vm_id: str, template_id: str):
        with self._conn() as db:
            row = db.execute('SELECT state FROM vms WHERE id=?', (vm_id,)).fetchone()
            if not row or row['state'] != 'REINSTALLING':
                raise StoreConflict('VM was not reserved for reinstall')
            db.execute("UPDATE vms SET template_id=?, state='READY' WHERE id=?", (template_id, vm_id))

    def queue_vm_delete(self, vm_id: str) -> dict:
        """Serialize deletion with every other VM operation, and block new jobs."""
        with self._conn() as db:
            vm = db.execute('SELECT state FROM vms WHERE id=?', (vm_id,)).fetchone()
            if not vm or vm['state'] not in {'READY', 'ERROR', 'DELETE_ERROR'}:
                raise StoreConflict('VM is not in a deletable state')
            job_id = self._new_job(db, vm_id, 'DELETE_VM')
            db.execute("UPDATE vms SET state='DELETING' WHERE id=?", (vm_id,))
        return self.get_job(job_id)

    def finish_vm_delete(self, vm_id: str) -> None:
        """Keep old jobs and an audit tombstone; allow reusing the VM name/path."""
        with self._conn() as db:
            vm = db.execute('SELECT * FROM vms WHERE id=?', (vm_id,)).fetchone()
            if not vm or vm['state'] != 'DELETING':
                raise StoreConflict('VM is not being deleted')
            db.execute('INSERT INTO vm_deletions VALUES (?,?,?,?)',
                       (vm_id, vm['name'], vm['vmx_path'], timestamp()))
            db.execute("UPDATE vms SET name=?, vmx_path=?, state='DELETED' WHERE id=?",
                       ('_deleted_' + vm_id, 'deleted://'+vm_id+'.vmx', vm_id))

    def queue_identity_repair(self, vm_id: str) -> dict:
        """Only recover a known complete clone rejected by the duplicate-ID guard."""
        with self._conn() as db:
            vm = db.execute('SELECT state FROM vms WHERE id=?', (vm_id,)).fetchone()
            if not vm or vm['state'] != 'ERROR':
                raise StoreConflict('Only ERROR VMs can be repaired')
            failed = db.execute('SELECT action,status,detail FROM jobs WHERE vm_id=? '
                                'ORDER BY created_at DESC LIMIT 1', (vm_id,)).fetchone()
            if not failed or (failed['action'] != 'CLONE_FULL' or failed['status'] != 'FAILED' or
                              'same explicit VM UUID/MAC' not in failed['detail']):
                raise StoreConflict('No eligible duplicate-identity clone failure to repair')
            job_id = self._new_job(db, vm_id, 'REPAIR_CLONE_IDENTITY')
        return self.get_job(job_id)

    def set_job_running(self, job_id: str) -> None:
        with self._conn() as db:
            db.execute("UPDATE jobs SET status='RUNNING', started_at=? WHERE id=? AND status='QUEUED'",
                       (timestamp(), job_id))


    def update_job_detail(self, job_id: str, detail: str) -> None:
        """Durable progress text for a running job and dashboard polling."""
        with self._conn() as db:
            db.execute("UPDATE jobs SET detail=? WHERE id=? AND status='RUNNING'",
                       (detail[:300], job_id))

    def finish_job(self, job_id: str, success: bool, detail: str = '') -> None:
        with self._conn() as db:
            db.execute('UPDATE jobs SET status=?, detail=?, completed_at=? WHERE id=?',
                       ('SUCCEEDED' if success else 'FAILED', detail, timestamp(), job_id))

    def set_vm_state(self, vm_id: str, state: str):
        with self._conn() as db:
            db.execute('UPDATE vms SET state=? WHERE id=?', (state, vm_id))

    def recover_interrupted(self):
        # No replay of destructive actions after host/agent restart.
        with self._conn() as db:
            db.execute("UPDATE jobs SET status='FAILED', detail='Host Agent restarted; inspect actual VM state', "
                       "completed_at=? WHERE status IN ('QUEUED','RUNNING')", (timestamp(),))
            db.execute("UPDATE vms SET state='ERROR' WHERE state='CREATING'")
            db.execute("UPDATE vms SET state='DELETE_ERROR' WHERE state='DELETING'")
            db.execute("UPDATE vms SET state='REINSTALL_ERROR' WHERE state='REINSTALLING'")
