"""Persistent paid-instance expiry with host-side power enforcement.

VMware power operations deliberately run outside HTTP request threads. One
process/one VM worker is required; updates and enforcement serialize on the
shared BusinessDb RLock to prevent a stale expiry from powering off a renewed VM.
"""
from __future__ import annotations
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def parse_utc(raw: str) -> str:
    if not isinstance(raw,str) or not raw.endswith('Z') or len(raw)>40:
        raise ValueError('expires_at must be UTC ISO8601 with a trailing Z')
    try:
        dt = datetime.fromisoformat(raw.replace('Z','+00:00'))
    except ValueError as exc:
        raise ValueError('Invalid expires_at') from exc
    if dt.tzinfo is None or dt.utcoffset().total_seconds()!=0:
        raise ValueError('expires_at must be UTC')
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')


def is_due(iso: str | None, now: float | None = None) -> bool:
    return bool(iso and datetime.fromisoformat(iso.replace('Z','+00:00')).timestamp() <= (now if now is not None else time.time()))


class ExpiryScheduler:
    """Host-process-only enforcement, never a Windows scheduled task.

    Every VM is checked independently. The 600-second grace window is persisted
    in SQLite, so a Host restart does not reset an already issued soft request.
    The BusinessDb lock serializes VMware *actions* with renewal, but is never
    held during the ten-minute grace period.
    """
    GRACE_SECONDS = 600
    HARD_RETRY_SECONDS = 60

    def __init__(self, business_db, store, vmrun, vms_root: Path, remote=None,
                 poll_seconds=10, clock=None):
        self.db = business_db
        self.store = store
        self.vmrun = vmrun
        self.vms_root = vms_root.resolve()
        self.remote = remote
        self.poll_seconds = poll_seconds
        self.clock = clock or time.time
        self.stop_flag = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self.run, name='VMO-expiry', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        if self.thread: self.thread.join(timeout=5)

    def run(self):
        while not self.stop_flag.is_set():
            try:self.tick()
            except Exception as exc:
                import logging
                logging.getLogger('vmo.expiry').exception('Expiry scheduler tick failed: %s',type(exc).__name__)
            self.stop_flag.wait(self.poll_seconds)

    def vmx(self, service):
        vm = self.store.get_vm(service['vm_id'])
        if not vm or vm.get('state')=='DELETED':return None
        expected = self.vms_root / vm['name'] / (vm['name']+'.vmx')
        if (Path(vm['vmx_path']) != expected or not expected.is_file() or
                not expected.resolve().is_relative_to(self.vms_root)):
            raise RuntimeError('Managed VMX invalid or missing')
        return expected

    def tick(self):
        # No ten-minute sleeps or waits here: new renewals can proceed promptly.
        # A global VM job should not be run concurrently with this worker.
        self.db.reconcile_provisioning(self.store)
        for snapshot in self.db.services():
            with self.db.lock:
                s = self.db.service(snapshot['service_ref'])  # reread to reject stale renewals
                if not s:continue
                now = int(self.clock())
                expired = is_due(s.get('expires_at'), now)
                suspended = s['status'] in ('SUSPENDED','TERMINATED')
                if not (expired or suspended or s['status']=='EXPIRED'):
                    continue
                if expired:
                    # Revoke client sessions and mark the service before VM power operations.
                    self.db.mark_expired(s['service_ref'])
                    s = self.db.service(s['service_ref'])
                    if self.remote:self.remote.revoke_app_sessions(s['service_ref'])
                if s['status'] not in ('EXPIRED','SUSPENDED','TERMINATED'):
                    continue
                try:
                    vmx = self.vmx(s)
                    if vmx is None:continue
                    if not self.vmrun.is_running(vmx):
                        if expired and s.get('expiry_status')!='ENFORCED':
                            self.db.mark_expiry_result(s['service_ref'],'ENFORCED',None)
                        continue
                    # Manual suspension/termination keeps its original immediate
                    # hard-stop policy, distinct from a paid expiry grace period.
                    if suspended:
                        self.vmrun.stop(vmx,'hard')
                        if self.vmrun.is_running(vmx):
                            raise RuntimeError('VM still running after suspension hard stop')
                        if expired:self.db.mark_expiry_result(s['service_ref'],'ENFORCED',None)
                        self.db.audit(s['service_ref'],'SUSPEND_POWER_OFF','completed')
                        continue
                    # Paid expiry: soft first, then hard only after >=600 seconds.
                    generation=s['expiry_generation']
                    soft_at=s.get('expiry_soft_requested_at')
                    if (soft_at is None or s.get('expiry_soft_generation') != generation
                            or s.get('expiry_status') == 'ENFORCED'):
                        if not self.db.mark_soft_shutdown(s['service_ref'],generation,now):
                            continue
                        try:
                            self.vmrun.stop(vmx,'soft')
                            self.db.audit(s['service_ref'],'EXPIRY_SOFT_STOP','requested')
                        except Exception as exc:
                            # Even if Tools refuses the soft shutdown, retain the
                            # grace countdown; never hard-stop before 10 minutes.
                            self.db.audit(s['service_ref'],'EXPIRY_SOFT_STOP',
                                          'failed:'+type(exc).__name__)
                        # Do not wait here; next poll checks power-off or grace.
                        continue
                    if now < soft_at + self.GRACE_SECONDS:
                        continue
                    last_hard=s.get('expiry_hard_attempted_at')
                    if last_hard is not None and now-last_hard < self.HARD_RETRY_SECONDS:
                        continue
                    if not self.db.mark_hard_shutdown_attempt(s['service_ref'],generation,now):
                        continue
                    self.vmrun.stop(vmx,'hard')
                    if self.vmrun.is_running(vmx):
                        raise RuntimeError('VM still running after expiry hard stop')
                    self.db.mark_expiry_result(s['service_ref'],'ENFORCED',None)
                    self.db.audit(s['service_ref'],'EXPIRY_HARD_STOP','completed')
                except Exception as exc:
                    if expired:
                        self.db.mark_expiry_result(s['service_ref'],'FAILED',type(exc).__name__)
                    self.db.audit(s['service_ref'],
                                  'EXPIRY_POWER_OFF' if expired else 'SUSPEND_POWER_OFF',
                                  'failed:'+type(exc).__name__)
