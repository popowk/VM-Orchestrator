"""Read-only host telemetry for the authenticated overview dashboard.

All gauges reflect host OS counters, not VM guest-level utilization.
Missing dependencies/counters are reported as unavailable rather than made up.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import threading
import time

try:
    import psutil
except ImportError:  # A partial installation must not prevent the Host starting.
    psutil = None

_NET_LOCK = threading.Lock()
_LAST_NET = None


def read_host_metrics(project_root: Path, vms_root: Path) -> dict:
    global _LAST_NET
    if psutil is None:
        return {"available": False, "message": "缺少 psutil，请修复宿主机 Python 依赖"}
    try:
        cpu = {
            "percent": round(psutil.cpu_percent(interval=0.1), 1),
            "logical_cores": psutil.cpu_count(logical=True),
            "physical_cores": psutil.cpu_count(logical=False),
        }
        ram = psutil.virtual_memory()
        memory = {"total": ram.total, "used": ram.total - ram.available,
                  "available": ram.available, "percent": round(ram.percent, 1)}
        disks = []
        seen = set()
        for label, path in (("程序所在磁盘", project_root), ("虚拟机所在磁盘", vms_root)):
            resolved = Path(path).resolve()
            volume = resolved.anchor or str(resolved)
            if volume.casefold() in seen:
                continue
            seen.add(volume.casefold())
            try:
                usage = psutil.disk_usage(volume)
                disks.append({"label": label, "volume": volume, "total": usage.total,
                              "used": usage.used, "free": usage.free,
                              "percent": round(usage.percent, 1)})
            except OSError:
                disks.append({"label": label, "volume": volume, "available": False})
        network = {"sample_ready": False, "upload_bps": None, "download_bps": None}
        try:
            counters = psutil.net_io_counters()
            now = time.monotonic()
            with _NET_LOCK:
                prev = _LAST_NET
                _LAST_NET = (now, counters.bytes_sent, counters.bytes_recv)
            if prev is not None and now > prev[0]:
                elapsed = now - prev[0]
                network = {
                    "sample_ready": True,
                    "upload_bps": round(max(0, counters.bytes_sent - prev[1]) / elapsed),
                    "download_bps": round(max(0, counters.bytes_recv - prev[2]) / elapsed),
                }
        except (OSError, AttributeError):
            pass
        return {
            "available": True, "cpu": cpu, "memory": memory,
            "disks": disks, "network": network,
            "uptime_seconds": max(0, int(time.time() - psutil.boot_time())),
            "host": socket.gethostname(), "sample_time": time.time(),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        return {"available": False, "message": "宿主机性能计数器暂不可用", "error_type": type(exc).__name__}
