from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    templates_root: Path
    db_path: Path
    vmrun_path: Path
    admin_key: str
    vms_root: Path = Path(r'D:\VMware\Customers')

    @classmethod
    def from_env(cls) -> 'Settings':
        return cls(
            templates_root=Path(os.environ.get('VMO_TEMPLATES_ROOT', r'D:\VMware\Template')),
            db_path=Path(os.environ.get('VMO_DB_PATH', r'D:\VMware\VM-Orchestrator\data\templates.sqlite3')),
            vmrun_path=Path(os.environ.get('VMO_VMRUN_PATH', r'C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe')),
            admin_key=os.environ.get('VMO_ADMIN_KEY', ''),
            vms_root=Path(os.environ.get('VMO_VMS_ROOT', r'D:\VMware\Customers')),
        )
