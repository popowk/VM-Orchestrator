"""One Python process, two strictly independent ASGI listeners.

Host admin (HTTP/8765) and Guest Gateway (HTTPS/8766) are NEVER mounted
on the same ASGI app or exposed via the same port. Use exactly one worker.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import ipaddress
import os
from pathlib import Path
import signal

import uvicorn


def build_servers(host_bind: str, host_port: int, guest_bind: str, guest_port: int,
                  certfile: str, keyfile: str) -> tuple[uvicorn.Server, uvicorn.Server]:
    private = ipaddress.ip_address(guest_bind)
    if not isinstance(private, ipaddress.IPv4Address) or not private.is_private or private.is_loopback or private.is_unspecified:
        raise ValueError('Guest address must be the specific private IPv4 assigned to the host')
    if guest_port == host_port:
        raise ValueError('Host and Guest listeners must never share a port')
    if not Path(certfile).is_file() or not Path(keyfile).is_file():
        raise ValueError('Guest TLS certificate or key is missing')
    if not os.environ.get('VMO_ADMIN_KEY') or len(os.environ['VMO_ADMIN_KEY']) < 32:
        raise ValueError('VMO_ADMIN_KEY must be initialized before launch')
    base = dict(workers=1, log_level='info', access_log=False)
    host = uvicorn.Server(uvicorn.Config('app.main:app', host=host_bind, port=host_port, **base))
    guest = uvicorn.Server(uvicorn.Config('app.guest_api:create_guest_app', factory=True,
                                        host=guest_bind, port=guest_port,
                                        ssl_certfile=certfile, ssl_keyfile=keyfile, **base))
    # Only the supervisor handles SIGINT/SIGTERM; otherwise concurrent servers
    # overwrite each other's process-global signal handlers.
    host.capture_signals = lambda: nullcontext()
    guest.capture_signals = lambda: nullcontext()
    return host, guest


async def run_pair(host: uvicorn.Server, guest: uvicorn.Server) -> int:
    """Start Guest TLS first, then the Host admin app that advertises its URL."""
    servers = (host, guest)
    tasks: list[asyncio.Task] = []
    stopping = False

    def shutdown(*_):
        nonlocal stopping
        stopping = True
        for server in servers:
            server.should_exit = True

    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            old_handlers[sig] = signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass
    try:
        # Guest Agent endpoint must be listening before the Host app advertises it.
        # No extra OS process or Uvicorn worker is created for either listener.
        guest_task = asyncio.create_task(guest.serve(), name='vmo-guest-https')
        tasks.append(guest_task)
        for _ in range(600):
            if guest.started or guest_task.done() or stopping:
                break
            await asyncio.sleep(0.1)
        if not guest.started or stopping:
            was_stopping = stopping
            shutdown()
            await asyncio.gather(*tasks, return_exceptions=True)
            return 0 if was_stopping else 1

        host_task = asyncio.create_task(host.serve(), name='vmo-host-http')
        tasks.append(host_task)
        # Terminate the pair if either listener stops/fails unexpectedly.
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        failed = not stopping
        for task in done:
            if task.cancelled() or task.exception() is not None:
                failed = True
        shutdown()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if any(isinstance(r, BaseException) for r in results):
            failed = True
        if not host.started or not guest.started:
            failed = True
        return 1 if failed else 0
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description='VMO unified Host + Guest backend')
    parser.add_argument('--host-bind', default='0.0.0.0')
    parser.add_argument('--host-port', type=int, default=8765)
    parser.add_argument('--guest-bind', required=True)
    parser.add_argument('--guest-port', type=int, default=8766)
    parser.add_argument('--certfile', required=True)
    parser.add_argument('--keyfile', required=True)
    options = parser.parse_args()
    servers = build_servers(options.host_bind, options.host_port,
                            options.guest_bind, options.guest_port, options.certfile, options.keyfile)
    return asyncio.run(run_pair(*servers))


if __name__ == '__main__':
    raise SystemExit(main())
