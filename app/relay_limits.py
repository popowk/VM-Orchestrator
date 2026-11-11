"""Per-Guest WSS HTTP stream scheduling.

An SPA can request dozens of static resources simultaneously. Queuing normal
browser bursts is safer than returning HTTP 429 when 4 old slots are busy.
Long-lived SSE connections have independent capacity and never starve assets.
This is an internal admission controller, not an authentication mechanism.
"""
from __future__ import annotations
import asyncio
import os


class RelayBusy(Exception):
    """An unusually large/slow request burst exhausted bounded waiting."""


class StreamLease:
    __slots__ = ('_slot', '_released')

    def __init__(self, slot: asyncio.Semaphore):
        self._slot = slot
        self._released = False

    def release(self):
        if not self._released:
            self._released = True
            self._slot.release()


class RelayStreamAdmission:
    # Defaults: 16 ordinary resources + 8 SSE readers per VM; Guest >=24.
    # Adjust both Host and Guest together; these are stream counts, not users.
    def __init__(self, asset_streams=None, event_streams=None, max_waiters=None,
                 asset_wait_seconds=45, event_wait_seconds=12):
        def limit(name,default,low,high):
            try: return max(low,min(high,int(os.environ.get(name,str(default)))))
            except ValueError:return default
        if asset_streams is None:asset_streams=limit('VMO_APP_ASSET_STREAMS',16,1,64)
        if event_streams is None:event_streams=limit('VMO_APP_SSE_STREAMS',8,1,32)
        if max_waiters is None:max_waiters=limit('VMO_APP_MAX_WAITERS',192,16,512)
        self.assets=asyncio.Semaphore(asset_streams)
        self.events=asyncio.Semaphore(event_streams)
        self.max_waiters=max_waiters
        self.asset_wait_seconds=asset_wait_seconds
        self.event_wait_seconds=event_wait_seconds
        self.waiters=0

    async def acquire(self,kind):
        if kind not in ('assets','events'):
            raise ValueError('Invalid application stream kind')
        if self.waiters>=self.max_waiters:
            raise RelayBusy('Too many pending app resources')
        self.waiters+=1
        try:
            sem=self.events if kind=='events' else self.assets
            timeout=self.event_wait_seconds if kind=='events' else self.asset_wait_seconds
            try:
                await asyncio.wait_for(sem.acquire(),timeout=timeout)
            except asyncio.TimeoutError as ex:
                raise RelayBusy('Application response queue timed out') from ex
            return StreamLease(sem)
        finally:
            self.waiters-=1
