import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import diskcache

T = TypeVar("T")

DAY = 86_400
WEEK = 7 * DAY


class Cache:
    """Read-through cache on SQLite (diskcache), with single-flight lookups.

    Local reads take well under a millisecond, so they run inline rather than
    in a thread. Concurrent misses for the same key share one `factory` call, so two
    requests that need the same upstream data (e.g. /analyze and /similar both need the
    recording) cost one rate-limited MusicBrainz request, not two. Failures are never
    cached because exceptions propagate. When every caller waiting on a lookup has gone away
    the lookup is cancelled. To scale out, swap this class for Redis;
    callers only use `get_or_set` and `put`.
    """

    def __init__(self, directory: str, size_limit: int):
        self._store = diskcache.Cache(directory, size_limit=size_limit, disk=diskcache.JSONDisk)  # JSON, not pickle (CVE-2025-69872)
        self._inflight: dict[str, asyncio.Future] = {}
        self._waiters: dict[str, int] = {}

    def put(self, key: str, value, ttl: int) -> None:
        """Store something we already have in hand (e.g. read off a search hit) unless the key is filled."""
        self._store.add(key, value, expire=ttl)

    async def get_or_set(self, key: str, ttl: int, factory: Callable[[], Awaitable[T]]) -> T:
        if (hit := self._store.get(key)) is not None:
            return hit
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fill(key, ttl, factory))
            self._inflight[key] = task
            task.add_done_callback(lambda t, k=key: self._done(k, t))
        self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            # shield: one caller giving up must not cancel the lookup the others still wait for
            return await asyncio.shield(task)
        finally:
            self._waiters[key] -= 1
            if not self._waiters[key]:
                del self._waiters[key]
                if not task.done():  # the last interested caller left (client cancelled): stop the work,
                    task.cancel()    # which also frees a queued rate-limiter slot for whoever is next
                    if self._inflight.get(key) is task:
                        del self._inflight[key]  # a caller arriving right now must start a fresh lookup

    async def _fill(self, key: str, ttl: int, factory: Callable[[], Awaitable[T]]) -> T:
        value = await factory()
        self._store.set(key, value, expire=ttl)
        return value

    def _done(self, key: str, task: asyncio.Future) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()  # mark retrieved: callers that gave up must not log "never retrieved"

    def close(self) -> None:
        self._store.close()