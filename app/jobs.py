"""One worker serializes VMware operations across VMs on this single host.

Do not run more than one uvicorn process/worker against the same instance.
"""
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import re
import sqlite3
from datetime import datetime,timezone
import os
import time
import uuid
import shutil
from .safety import assert_powered_off, assert_safe_vm_delete_dir, MAX_SNAPSHOTS
from .storage import TemplateStore
from .fleet import FleetStore
from .vmrun import Vmrun, VmrunError
from .vnc_screen import enable_vnc, vnc_config
from .unattend import prepare_clone as prepare_unattend_clone, cleanup_clone_media
from .diagnostics import diagnose

log = logging.getLogger(__name__)
SHUTDOWN_TIMEOUT_SECONDS = 180
SHUTDOWN_POLL_SECONDS = 2


def verify_distinct_vmx_identities(source: Path, clone: Path) -> None:
    """Fail closed on identical explicit VMware UUID / MAC settings.

    Absent fields can be generated on first boot, so this is a duplicate detector,
    not a proof of independent guest Windows SID, credentials or guest identity.
    """
    key_re = re.compile(r'^\s*([^#=\s]+)\s*=\s*"([^"\r\n]*)"', re.I)
    def ids(path):
        found = {}
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            m = key_re.match(line)
            if not m:
                continue
            k = m.group(1).casefold()
            if k in {'uuid.bios', 'uuid.location', 'vc.uuid'} or re.fullmatch(r'ethernet\d+\.(?:generatedaddress|address)', k):
                found[k] = m.group(2).casefold()
        return found
    source_ids, clone_ids = ids(source), ids(clone)
    for key in (source_ids.keys() & clone_ids.keys()):
        if source_ids[key] and source_ids[key] == clone_ids[key]:
            raise VmrunError('Clone has the same explicit VM UUID/MAC as template; do not boot')





def prepare_cloned_vmx_identity(vmx: Path) -> None:
    """Reset VMware-specific identifiers ONLY in an offline clone, never the template.

    VMware Workstation creates its own BIOS UUID and generated MAC at first boot.
    Keep every other VMX setting, including the virtual disk paths and adapter types.
    Use bytes to preserve unknown/non-UTF8 VMX encoding and line endings.
    """
    if not vmx.is_file() or vmx.is_symlink():
        raise VmrunError('Cloned VMX missing or unsafe')
    old = vmx.read_bytes()
    if not old.strip():
        raise VmrunError('Cloned VMX is empty')
    key_re = re.compile(rb'^\s*([a-zA-Z0-9_.]+)\s*=')
    mac_key_re = re.compile(rb'^ethernet(\d+)\.(addresstype|address|generatedaddress|generatedaddressoffset)$', re.I)
    filtered = []
    adapter_ids = set()
    for line in old.splitlines(keepends=True):
        match = key_re.match(line)
        if match:
            key = match.group(1).lower()
            mac = mac_key_re.fullmatch(key)
            if mac:
                adapter_ids.add(int(mac.group(1)))
            if key in (b'uuid.bios', b'uuid.location', b'uuid.action', b'vc.uuid') or mac:
                continue
        filtered.append(line)

    # Force newly generated IDs; no first-boot "I moved it" choice is needed.
    eol = b'\r\n' if b'\r\n' in old else b'\n'
    new = b''.join(filtered)
    if new and not new.endswith((b'\n', b'\r')):
        new += eol
    new += b'uuid.action = "create"' + eol
    for adapter in sorted(adapter_ids):
        new += f'ethernet{adapter}.addressType = "generated"'.encode('ascii') + eol

    if new == old:
        return
    backup = vmx.with_suffix('.vmx.pre-identity-fix.bak')
    if backup.exists():
        if backup.read_bytes() != old:
            # This backup may be from a previous repair; do not silently overwrite it.
            raise VmrunError('Identity backup already exists and differs; inspect VMX manually')
    else:
        backup.write_bytes(old)
    tmp = vmx.with_suffix('.vmx.identity.tmp')
    try:
        tmp.write_bytes(new)
        os.replace(tmp, vmx)
    finally:
        tmp.unlink(missing_ok=True)



class JobRunner:
    def __init__(self, store: TemplateStore, vmrun: Vmrun, secret_root: Path | None = None, admin_key: str = ""):
        self.store = store
        self.vmrun = vmrun
        self.secret_root = secret_root
        self.admin_key = admin_key
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='vmware-job')

    def submit(self, job_id: str, vm_id: str, command: str, *, src: Path | None = None,
               name: str | None = None, snap: str | None = None, mode: str = 'soft',
               start_after_clone: bool = False, enable_screen: bool = False,
               unattended_oobe: bool = False,
               sibling_paths: list[Path] | None = None, vms_root: Path | None = None,
               reinstall_template_id: str | None = None, fleet: FleetStore | None = None,
               guest_gateway_url: str = "", guest_ca_path: Path | None = None):
        def work():
            self.store.set_job_running(job_id)
            try:
                vm = self.store.get_vm(vm_id)
                if not vm:
                    raise VmrunError('VM record missing')
                vmx = Path(vm['vmx_path'])
                # Re-check lifecycle at execution time, not merely when queued.
                # This closes the gap between an old queued START and the expiry job.
                if command in ('START','REINSTALL_FROM_TEMPLATE','SNAPSHOT_RESTORE'):
                    with sqlite3.connect(str(self.store.db_path)) as d:
                        try: row=d.execute('SELECT status,expires_at FROM business_services WHERE vm_id=?',(vm_id,)).fetchone()
                        except sqlite3.OperationalError as exc:
                            if 'no such table: business_services' in str(exc).lower():
                                row=None
                            else:
                                raise VmrunError('Cannot verify paid-instance lifecycle before power operation') from exc
                    if row and (row[0]!='ACTIVE' or (row[1] and datetime.fromisoformat(row[1].replace('Z','+00:00')).timestamp()<=time.time())):
                        raise VmrunError('Cannot power on or restore expired/suspended customer VM')
                if command == 'REINSTALL_FROM_TEMPLATE':
                    if not src or not vms_root or not reinstall_template_id or not fleet:
                        raise VmrunError('Missing reinstall source or destination')
                    old_template_id = vm['template_id']
                    backup_path = ''
                    staging_path = vms_root / '.vmo-staging' / (vm_id + '-' + job_id)
                    staged_vmx = staging_path / (name + '.vmx')
                    original_dir = vmx.parent
                    backup_dir = vms_root / '.vmo-reinstall-backups' / (vm['name']+'-'+job_id)
                    moved_old = False
                    moved_stage = False
                    try:
                        self.store.update_job_detail(job_id, 'Preflight: VM must be fully off and unmounted')
                        assert_safe_vm_delete_dir(vmx, vms_root)
                        assert_powered_off(vmx, self.vmrun, strict_locks=True)
                        if not src.is_file() or self.vmrun.is_running(src) or list(src.parent.glob('*.lck')) or list(src.parent.glob('*.vmss')):
                            raise VmrunError('Golden template must be off and unlocked')
                        if staging_path.exists() or backup_dir.exists():
                            raise VmrunError('Reinstall staging or backup already exists; inspect manually')
                        staging_path.parent.mkdir(parents=True,exist_ok=True)
                        backup_dir.parent.mkdir(parents=True,exist_ok=True)
                        staging_path.mkdir()
                        fleet.reinstall_record(job_id,vm_id,old_template_id,reinstall_template_id,'QUEUED')
                        self.store.update_job_detail(job_id, 'Cloning golden template into isolated staging directory')
                        self.vmrun.clone_full(src,staged_vmx,name)
                        if not staged_vmx.is_file():
                            raise VmrunError('Reinstall clone completed without a VMX file')
                        prepare_cloned_vmx_identity(staged_vmx)
                        verify_distinct_vmx_identities(src,staged_vmx)
                        if staged_vmx.parent != staging_path or staged_vmx.name != name + '.vmx':
                            raise VmrunError('Unexpected staging VMX location')
                        if any(staging_path.rglob('*.lck')) or any(staging_path.rglob('*.vmss')):
                            raise VmrunError('Staged VM has unsafe lock/suspend files')
                        # A second check closes the race where GUI powers on the old VM.
                        assert_powered_off(vmx, self.vmrun, strict_locks=True)
                        self.store.update_job_detail(job_id,'Preserving original VM directory for rollback')
                        os.replace(original_dir,backup_dir)
                        moved_old = True
                        backup_path = str(backup_dir)
                        try:
                            os.replace(staging_path, original_dir)
                            moved_stage = True
                        except BaseException:
                            # Restore old VM before reporting failure.
                            os.replace(backup_dir,original_dir)
                            moved_old = False
                            raise
                        # A moved VMX can contain absolute references to the temporary stage path.
                        # Rebase only exact stage-prefix references in text VMX, not customer files.
                        old_vmx_bytes = vmx.read_bytes()
                        old_stage = str(staging_path).encode('utf-8')
                        new_dir = str(original_dir).encode('utf-8')
                        if old_stage in old_vmx_bytes:
                            vmx.write_bytes(old_vmx_bytes.replace(old_stage,new_dir))
                        # Preserve the OLD encrypted setup credential together with the old VM.
                        if self.secret_root:
                            old_secret=self.secret_root/(vm_id+'.secret')
                            if old_secret.is_file():
                                os.replace(old_secret,backup_dir/'vmo-old-secret.encrypted')
                        fleet.revoke_vm(vm_id)
                        self.store.finish_reinstall(vm_id,reinstall_template_id)
                        if unattended_oobe:
                            self.store.update_job_detail(job_id,'Preparing new per-VM Windows OOBE answer media')
                            bootstrap=None
                            if guest_gateway_url and guest_ca_path and guest_ca_path.is_file():
                                token=fleet.create_enrollment(vm_id,lifetime_minutes=1440)
                                bootstrap={'server_url':guest_gateway_url,'vm_id':vm_id,
                                           'enrollment_token':token,'ca_pem':guest_ca_path.read_text(encoding='ascii')}
                            prepare_unattend_clone(vmx,vm_id,self.secret_root,self.admin_key,bootstrap=bootstrap)
                        if enable_screen:
                            used=set()
                            for other in self.store.list_vms():
                                path=Path(other['vmx_path'])
                                if path!=vmx and path.is_file():
                                    vc=vnc_config(path)
                                    if vc.get('port','').isdigit(): used.add(int(vc['port']))
                            enable_vnc(vmx,used)
                        fleet.reinstall_record(job_id,vm_id,old_template_id,reinstall_template_id,'SUCCEEDED',backup_path)
                        self.store.finish_job(job_id,True,'Reinstalled from sealed template; old VM kept as backup. New guest must register independently; VM is OFF and OOBE is NOT verified.')
                        return
                    except Exception:
                        if moved_old and not moved_stage and backup_dir.exists() and not original_dir.exists():
                            os.replace(backup_dir,original_dir)
                            moved_old = False
                        # If a new VM was already installed, leave it and backup for manual recovery.
                        if moved_stage:
                            self.store.set_vm_state(vm_id,'REINSTALL_ERROR')
                        else:
                            self.store.set_vm_state(vm_id,'READY')
                        fleet.reinstall_record(job_id,vm_id,old_template_id,reinstall_template_id,'FAILED',backup_path) if fleet.reinstalls(vm_id) and any(x['job_id']==job_id for x in fleet.reinstalls(vm_id)) else None
                        raise
                if command == 'DELETE_VM':
                    if vms_root is None:
                        raise VmrunError('VM delete root not supplied')
                    # Reverify: the state may change between HTTP acceptance and worker execution.
                    assert_safe_vm_delete_dir(vmx, vms_root, allow_missing_vmx=True)
                    assert_powered_off(vmx, self.vmrun, strict_locks=True)
                    self.store.update_job_detail(job_id, 'Permanently deleting the VM directory; no templates are touched')
                    # Never follow symlinks or NTFS reparse points outside the managed folder.
                    if vmx.parent.exists():
                        shutil.rmtree(vmx.parent)
                    if self.secret_root is not None:
                        (self.secret_root / (vm_id + '.secret')).unlink(missing_ok=True)
                    if fleet: fleet.revoke_vm(vm_id)
                    self.store.finish_vm_delete(vm_id)
                    self.store.finish_job(job_id, True, 'VM directory permanently removed; inventory archived')
                    return
                if command == 'CLONE_FULL':
                    if src is None or not name:
                        raise VmrunError('Invalid clone request')
                    if vmx.parent.exists():
                        raise VmrunError('Destination directory already exists; refusing overwrite')
                    vmx.parent.mkdir(parents=False, exist_ok=False)
                    if self.vmrun.is_running(src) or list(src.parent.glob('*.lck')) or list(src.parent.glob('*.vmss')):
                        raise VmrunError('Source template is running, locked or suspended')
                    self.vmrun.clone_full(src, vmx, name)
                    if not vmx.is_file():
                        raise VmrunError('Clone command completed without creating destination VMX')
                    prepare_cloned_vmx_identity(vmx)
                    verify_distinct_vmx_identities(src, vmx)
                    self.store.set_vm_state(vm_id, 'READY')
                    if unattended_oobe:
                        if not self.secret_root:
                            raise VmrunError('Host provisioning storage unavailable')
                        self.store.update_job_detail(job_id, 'Creating unique per-VM OOBE ISO and encrypted setup credentials')
                        bootstrap = None
                        if fleet and guest_gateway_url and guest_ca_path and guest_ca_path.is_file():
                            token = fleet.create_enrollment(vm_id)
                            bootstrap = {'server_url':guest_gateway_url, 'vm_id':vm_id,
                                         'enrollment_token':token, 'ca_pem':guest_ca_path.read_text(encoding='ascii')}
                        prepare_unattend_clone(vmx, vm_id, self.secret_root, self.admin_key, bootstrap=bootstrap)
                    if enable_screen:
                        used = set()
                        for other in sibling_paths or []:
                            if other.is_file():
                                config = vnc_config(other)
                                if config.get('port', '').isdigit():
                                    used.add(int(config['port']))
                        self.store.update_job_detail(job_id, 'Enabling loopback screenshot for new VM')
                        enable_vnc(vmx, used)
                    if start_after_clone:
                        self.store.update_job_detail(job_id, 'Starting the newly cloned VM')
                        self.vmrun.start(vmx)
                        if not self.vmrun.is_running(vmx):
                            raise VmrunError('Clone exists, but VMware start was not confirmed. Inspect VM before retrying')
                        self.store.finish_job(job_id, True, 'Full clone READY; VM powered on. OOBE is NOT verified; inspect screenshot/status.')
                        return
                elif command == 'UNATTEND_CLEANUP':
                    if not self.secret_root:
                        raise VmrunError('Host provisioning storage unavailable')
                    state = diagnose(vmx, self.vmrun)
                    if not state['offline_vmx_edit_possible']:
                        raise VmrunError(state['reason'])
                    cleanup_clone_media(vmx, self.secret_root, vm_id)
                    self.store.finish_job(job_id, True, 'Provisioning ISO detached and deleted; host-encrypted account credential retained')
                    return
                elif command == 'REPAIR_CLONE_IDENTITY':
                    if src is None:
                        raise VmrunError('Template reference required for repair')
                    if not vmx.is_file():
                        raise VmrunError('Cloned VMX missing; repair not possible')
                    if self.vmrun.is_running(vmx) or list(vmx.parent.glob('*.lck')) or list(vmx.parent.glob('*.vmss')):
                        raise VmrunError('Cannot repair a powered-on, locked or suspended VM')
                    prepare_cloned_vmx_identity(vmx)
                    verify_distinct_vmx_identities(src, vmx)
                    self.store.set_vm_state(vm_id, 'READY')
                elif command == 'START':
                    if self.vmrun.is_running(vmx):
                        raise VmrunError('VM already running')
                    self.vmrun.start(vmx)
                elif command == 'STOP':
                    if not self.vmrun.is_running(vmx):
                        raise VmrunError('VM is not running')
                    self.vmrun.stop(vmx, mode)
                elif command == 'SNAPSHOT_CREATE':
                    assert_powered_off(vmx, self.vmrun)
                    snapshots = self.vmrun.snapshots(vmx)
                    if len(snapshots) >= MAX_SNAPSHOTS:
                        raise VmrunError(f'Maximum {MAX_SNAPSHOTS} snapshots per VM; delete an old snapshot first')
                    if snap in snapshots:
                        raise VmrunError('Snapshot name already exists')
                    self.vmrun.snapshot_create(vmx, snap)
                elif command in ('SNAPSHOT_RESTORE', 'SNAPSHOT_DELETE'):
                    assert_powered_off(vmx, self.vmrun)
                    self.store.update_job_detail(job_id, 'Checking target snapshot')
                    snap_list = self.vmrun.snapshots(vmx)
                    if snap not in snap_list:
                        raise VmrunError('Snapshot not found')
                    if snap_list.count(snap) != 1:
                        raise VmrunError('Snapshot name is ambiguous; use a unique snapshot name')
                    if command == 'SNAPSHOT_RESTORE':
                        self.store.update_job_detail(job_id, 'Restoring snapshot after powered-off verification')
                        self.vmrun.snapshot_restore(vmx, snap)
                    else:
                        self.store.update_job_detail(job_id, 'Deleting snapshot after powered-off verification')
                        self.vmrun.snapshot_delete(vmx, snap)
                else:
                    raise VmrunError('Unsupported action')
                self.store.finish_job(job_id, True, 'Completed')
            except Exception as exc:
                if command == 'CLONE_FULL':
                    self.store.set_vm_state(vm_id, 'ERROR')
                if command == 'DELETE_VM':
                    self.store.set_vm_state(vm_id, 'DELETE_ERROR')
                # Avoid printing subprocess output, VM/guest data and sensitive paths.
                detail = str(exc) if isinstance(exc, VmrunError) else 'Unexpected host operation failure'
                log.exception('VMware operation %s failed for job %s', command, job_id)
                self.store.finish_job(job_id, False, detail[:300])
        self.pool.submit(work)
