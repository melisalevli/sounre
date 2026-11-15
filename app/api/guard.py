import asyncio
from collections.abc import Awaitable
from typing import Protocol, TypeVar

from ..errors import ClientGone

T = TypeVar("T")
POLL = 0.25  # seconds between disconnect checks while the work is still running


class _Request(Protocol):
    async def is_disconnected(self) -> bool: ...


async def until_disconnect(request: _Request, work: Awaitable[T]) -> T:
    """Await `work`, but cancel it as soon as the client hangs up.

    Starlette keeps running a handler after its client has gone, so without this an abandoned
    request would keep its place in the 1 req/s MusicBrainz queue. Work that finishes within one
    poll interval (cache hits, fast upstreams) never even checks the connection.
    """
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=POLL)
            if done:
                return task.result()
            if await request.is_disconnected():
                raise ClientGone
    finally:
        if not task.done():
            task.cancel()
