import asyncio
import time

from .errors import AppError

MAX_QUEUE = 10  # about 10 s of waiting at 1 request/s; beyond that, fail fast instead of piling up


class RateLimiter:
    """Spaces calls at least `interval` seconds apart. Waiters are served FIFO; the line is capped."""

    def __init__(self, interval: float, max_queue: int = MAX_QUEUE):
        self._interval = interval
        self._max_queue = max_queue
        self._queued = 0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        if self._queued >= self._max_queue:
            raise AppError(503, "busy")  # one abuser must not turn everyone else's request into a 10 s+ wait
        self._queued += 1
        try:
            async with self._lock:
                now = time.monotonic()
                start = max(now, self._next)
                if start > now:
                    await asyncio.sleep(start - now)
                self._next = start + self._interval
        finally:
            self._queued -= 1
