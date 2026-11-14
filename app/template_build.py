"""Dedicated, non-destructive VMware Workstation golden-template build workflow.

Every build is isolated under <installed VMO root>\template_build. Operator can start a
new Windows ISO VM or full-clone an existing SEALED_ATTESTED template. A source
build is never edited or moved when publishing; publishing uses full clone
into a NEW official template directory, registering only a CANDIDATE. The
operator still must run Sysprep and attest the official golden image before
it is eligible to provision customers. No existing customer VM or template
is migrated or overwritten.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime,timezone

from .safety import _is_reparse_point
from .power_state import suspend_evidence


class TemplateBuildError(ValueError):
    pass


_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_name(value: str) -> str:
    name=str(value).strip()
    if not _NAME.fullmatch(name):
        raise TemplateBuildError('模板制作名称只允许1-48个英文、数字、_和-')
    return name


def isolate_roots(build_root: Path, templates_root: Path, vms_root: Path) -> None:
    roots=[build_root.resolve(strict=False),templates_root.resolve(strict=False),
           vms_root.resolve(strict=False)]
    for i in range(3):
        for j in range(i+1,3):
            a,b=roots[i],roots[j]
            if a==b or a.is_relative_to(b) or b.is_relative_to(a):
                raise TemplateBuildError('黄金模板制作目录、正式模板目录、客户机目录必须完全分离')


def require_offline(vmx: Path, vmrun) -> None:
    if not vmx.is_file() or _is_reparse_point(vmx) or _is_reparse_point(vmx.parent):
        raise TemplateBuildError('模板 VMX 文件不存在或使用了危险的链接目录')
    if vmrun.is_running(vmx):
        raise TemplateBuildError('模板仍在运行，请正常关机后操作')
    if list(vmx.parent.glob('*.lck')) or list(vmx.parent.glob('*.vmss')):
        raise TemplateBuildError('VMware 存在锁/挂起文件，请在 VMware 中确认，不能强行发布')
    if suspend_evidence(vmx)['state']!='clear':
        raise TemplateBuildError('VMX 仍有未确认的挂起引用，不能发布')


def iso_vmx(name: str, iso: Path, *, memory_mb: int, cpus: int) -> str:
    valid_name(name)
    if not (2048<=memory_mb<=32768 and memory_mb%4==0 and cpus in (1,2,4,6,8,12,16)):
        raise TemplateBuildError('模板内存或 CPU 参数无效')
    if '"' in str(iso) or '\r' in str(iso) or '\n' in str(iso):
        raise TemplateBuildError('ISO 路径不能包含引号或换行')
    # No automatic NIC connectivity: prevent a new, unfinished template from
    # accidentally accessing customer or management networks.
    return '\n'.join([
        '.encoding = "UTF-8"',
        'config.version = "8"',
        'virtualHW.version = "16"',
        f'displayName = "{name}"',
        'guestOS = "windows9-64"',
        'firmware = "efi"',
        f'memsize = "{memory_mb}"',
        f'numvcpus = "{cpus}"',
        'scsi0.present = "TRUE"',
        'scsi0.virtualDev = "lsilogic"',
        'scsi0:0.present = "TRUE"',
        f'scsi0:0.fileName = "{name}.vmdk"',
        'scsi0:0.deviceType = "scsi-hardDisk"',
        'ide1:0.present = "TRUE"',
        'ide1:0.deviceType = "cdrom-image"',
        f'ide1:0.fileName = "{str(iso).replace(chr(92), "/")}"',
        'ide1:0.startConnected = "TRUE"',
        'ethernet0.present = "FALSE"',
        'usb.present = "TRUE"',
        'sound.present = "FALSE"',
        'checkpoint.vmState = ""',
        'uuid.action = "create"',
        ''])


class TemplateBuildManager:
    def __init__(self, root: Path, templates_root: Path, vms_root: Path,
                 db_path: Path, store, vmrun, executor):
        isolate_roots(root,templates_root,vms_root)
        self.root=root
        self.templates_root=templates_root
        self.vms_root=vms_root
        self.db_path=db_path
        self.store=store
        self.vmrun=vmrun
        self.executor=executor
        self._guard=threading.Lock()
        self.root.mkdir(parents=True,exist_ok=True)
        self.templates_root.mkdir(parents=True,exist_ok=True)
        if _is_reparse_point(self.root) or _is_reparse_point(self.templates_root):
            raise TemplateBuildError('模板目录不允许符号链接或 Windows Junction')
        with self._db() as d:
            d.execute('''CREATE TABLE IF NOT EXISTS template_builds (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                mode TEXT NOT NULL, source_vmx TEXT NOT NULL DEFAULT '',
                iso_path TEXT NOT NULL DEFAULT '', memory_mb INTEGER NOT NULL,
                cpus INTEGER NOT NULL, disk_gb INTEGER NOT NULL,
                status TEXT NOT NULL, vmx_path TEXT NOT NULL,
                official_template_id TEXT, error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            d.execute("""UPDATE template_builds SET status='INTERRUPTED',
                         error='宿主服务重启，文件已保留，请在 VMware 中人工核实'
                         WHERE status IN ('CREATING','PUBLISHING')""")

    def _db(self):
        from contextlib import contextmanager
        @contextmanager
        def connect():
            d=sqlite3.connect(self.db_path,timeout=20)
            d.row_factory=sqlite3.Row
            try:
                yield d
                d.commit()
            finally:
                d.close()
        return connect()

    def list(self) -> list[dict]:
        with self._db() as d:
            return [dict(r) for r in d.execute(
                'SELECT * FROM template_builds ORDER BY created_at DESC LIMIT 100')]

    def get(self, key: str) -> dict | None:
        with self._db() as d:
            row=d.execute('SELECT * FROM template_builds WHERE id=?',(key,)).fetchone()
            return dict(row) if row else None

    def create(self, *, name: str, mode: str, source_vmx: Path | None = None,
               iso_path: Path | None = None, memory_mb: int = 8192,
               cpus: int = 4, disk_gb: int = 80) -> dict:
        name=valid_name(name)
        if mode not in ('iso','clone'):
            raise TemplateBuildError('制作方式必须为 Windows ISO 或现有模板完整克隆')
        if not (40<=disk_gb<=1024 and 2048<=memory_mb<=32768 and cpus in (1,2,4,6,8,12,16)):
            raise TemplateBuildError('系统盘需40–1024GB，内存2–32GB，CPU需1/2/4/6/8/12/16核')
        if memory_mb%4:
            raise TemplateBuildError('虚拟机内存必须为4MB的整数倍')
        root=self.root/name
        if root.exists() or root.is_symlink() or _is_reparse_point(root):
            raise TemplateBuildError('制作目录已存在，为保护历史数据不能覆盖')
        if mode=='iso':
            if not iso_path or not iso_path.is_absolute() or iso_path.suffix.lower()!='.iso' or not iso_path.is_file():
                raise TemplateBuildError('请选择宿主机上真实存在的 Windows 安装 ISO 完整路径')
            if _is_reparse_point(iso_path):
                raise TemplateBuildError('ISO 是符号链接或重解析文件，拒绝使用')
            source=''
        else:
            if not source_vmx:
                raise TemplateBuildError('请选择已有的已封存黄金模板')
            source_vmx=source_vmx.resolve(strict=True)
            if not source_vmx.is_relative_to(self.templates_root.resolve(strict=True)):
                raise TemplateBuildError('克隆来源必须是正式模板库下的模板')
            require_offline(source_vmx,self.vmrun)
            source=str(source_vmx)
        key=str(uuid.uuid4())
        vmx=root/(name+'.vmx')
        with self._guard, self._db() as d:
            d.execute('BEGIN IMMEDIATE')
            if d.execute('SELECT 1 FROM template_builds WHERE name=?',(name,)).fetchone():
                raise TemplateBuildError('此制作名称已经使用过，为保护历史项目请使用新名称')
            d.execute('''INSERT INTO template_builds
                (id,name,mode,source_vmx,iso_path,memory_mb,cpus,disk_gb,status,
                 vmx_path,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key,name,mode,source,str(iso_path or ''),memory_mb,cpus,disk_gb,
                 'CREATING',str(vmx),_now(),_now()))
        self.executor.submit(self._create_job,key)
        return self.get(key)

    def _status(self, key: str, status: str, error: str='',
                official_template_id: str | None = None) -> None:
        with self._db() as d:
            d.execute('''UPDATE template_builds SET status=?,error=?,updated_at=?,
                        official_template_id=COALESCE(?,official_template_id)
                        WHERE id=?''',(status,error[:600],_now(),official_template_id,key))

    def _create_job(self, key: str) -> None:
        record=self.get(key)
        if not record:return
        folder=self.root/record['name']
        try:
            folder.mkdir(parents=False,exist_ok=False)
            vmx=folder/(record['name']+'.vmx')
            if record['mode']=='clone':
                source=Path(record['source_vmx'])
                require_offline(source,self.vmrun)
                self.vmrun.clone_full(source,vmx,record['name'])
                if not vmx.is_file():
                    raise TemplateBuildError('VMware full clone 未生成 VMX，请检查 VMware 日志')
            else:
                iso=Path(record['iso_path'])
                if not iso.is_file():
                    raise TemplateBuildError('Windows ISO 已移动或删除')
                vdisk=self.vmrun.executable.parent/'vmware-vdiskmanager.exe'
                if not vdisk.is_file():
                    raise TemplateBuildError('未找到 VMware vmware-vdiskmanager.exe，不能创建全新虚拟磁盘')
                disk=folder/(record['name']+'.vmdk')
                result=subprocess.run(
                    [str(vdisk),'-c','-s',str(record['disk_gb'])+'GB',
                     '-a','lsilogic','-t','0',str(disk)],
                    capture_output=True,text=True,timeout=900,shell=False,
                    check=False)
                if result.returncode!=0 or not disk.is_file():
                    raise TemplateBuildError('vmware-vdiskmanager 创建系统盘失败，请检查宿主机 VMware 日志')
                vmx.write_text(iso_vmx(record['name'],iso,
                    memory_mb=record['memory_mb'],cpus=record['cpus']),
                    encoding='utf-8')
            self._status(key,'READY')
        except Exception as exc:
            # Keep partial disk artifacts for manual investigation, never
            # discard or reuse the same build folder after a VMware failure.
            self._status(key,'FAILED_REVIEW',str(exc))

    def publish(self, key: str) -> dict:
        with self._guard,self._db() as d:
            d.execute('BEGIN IMMEDIATE')
            row=d.execute('SELECT * FROM template_builds WHERE id=?',(key,)).fetchone()
            if not row or row['status']!='READY':
                raise TemplateBuildError('只能发布已完成制作且未发布过的模板')
            src=Path(row['vmx_path'])
            if src!=self.root/row['name']/(row['name']+'.vmx'):
                raise TemplateBuildError('来源路径不在隔离制作目录')
            require_offline(src,self.vmrun)
            # Block installer media on every controller type, including an
            # ISO inherited from a cloned existing template. Merely toggling
            # startConnected off is not sufficient: detach it completely.
            vmx_text=src.read_text(encoding='utf-8-sig',errors='replace')
            if re.search(r'(?im)^\s*(?:ide|sata|scsi)\d+:\d+\.fileName\s*=\s*"[^"]+\.iso"\s*$',vmx_text):
                raise TemplateBuildError('模板 VMX 仍引用 ISO 镜像；请在 VMware 中移除/清空 ISO 路径后再发布')
            destination=self.templates_root/row['name']/(row['name']+'.vmx')
            if destination.parent.exists() or destination.parent.is_symlink():
                raise TemplateBuildError('正式模板目录同名项目已存在，不能覆盖')
            d.execute("UPDATE template_builds SET status='PUBLISHING',updated_at=? WHERE id=?",
                      (_now(),key))
        self.executor.submit(self._publish_job,key)
        return self.get(key)

    def _publish_job(self, key: str) -> None:
        record=self.get(key)
        if not record:return
        source=Path(record['vmx_path'])
        destination=self.templates_root/record['name']/(record['name']+'.vmx')
        try:
            require_offline(source,self.vmrun)
            if destination.parent.exists():
                raise TemplateBuildError('正式模板目标目录已有文件')
            destination.parent.mkdir(parents=False,exist_ok=False)
            self.vmrun.clone_full(source,destination,record['name'])
            if not destination.is_file():
                raise TemplateBuildError('发布克隆结束后找不到正式模板 VMX')
            require_offline(destination,self.vmrun)
            saved=self.store.add(record['name'],destination)
            self._status(key,'PUBLISHED_CANDIDATE',official_template_id=saved['id'])
        except Exception as exc:
            self._status(key,'FAILED_REVIEW','正式模板副本可能已部分生成，保留文件供人工核查：'+str(exc))
