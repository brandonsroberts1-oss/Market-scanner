"""Per-source request pacing.

Free market-data endpoints throttle by IP. A single request always succeeds,
which is why a connectivity check can pass while the app sees nothing: the app
was firing hundreds of requests in a few seconds and being throttled from the
second or third onward.

This limits both how many requests are in flight at once and how closely they
are spaced, and backs off when a server says to.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

log = logging.getLogger(__name__)


class SourcePaused(RuntimeError):
    """Raised instead of waiting out a long throttle penalty.

    Blocking every caller until a 30-second penalty expires is how a rate limit
    turns into a scan that never finishes. Callers should treat this as "this
    source is unavailable right now" and move on to another one.
    """


class RateLimiter:
    """Concurrency cap plus a minimum gap between request starts."""

    # Waiting longer than this for a slot is never worth it: another source can
    # answer sooner, and the caller has a deadline.
    MAX_WAIT = 2.0

    def __init__(self, name: str, max_concurrent: int = 4, min_interval: float = 0.0):
        self.name = name
        self.min_interval = min_interval
        self._sem = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0
        self._penalty_until = 0.0

    async def __aenter__(self):
        # Check before queueing: if the source is serving a throttle penalty,
        # say so immediately rather than holding the caller.
        if self.paused_for > self.MAX_WAIT:
            raise SourcePaused(
                f"{self.name} is rate limited for another "
                f"{self.paused_for:.0f}s")

        await self._sem.acquire()
        try:
            async with self._lock:
                now = time.monotonic()
                # A 429 penalty applies to every caller, not just the one that
                # hit it.
                wait = max(self._next_allowed - now, self._penalty_until - now, 0.0)
                if wait > self.MAX_WAIT:
                    raise SourcePaused(
                        f"{self.name} is rate limited for another {wait:.0f}s")
                self._next_allowed = max(now, self._next_allowed,
                                         self._penalty_until) + self.min_interval
        except BaseException:
            self._sem.release()
            raise
        if wait > 0:
            await asyncio.sleep(wait)
        return self

    async def __aexit__(self, *exc_info):
        self._sem.release()
        return False

    def penalise(self, seconds: float) -> None:
        """Hold every request on this source for a while after a 429."""
        seconds = max(0.5, min(seconds, 120.0))
        target = time.monotonic() + seconds
        if target > self._penalty_until:
            self._penalty_until = target
            log.warning("%s asked us to slow down; pausing that source for %.0fs",
                        self.name, seconds)

    @property
    def paused_for(self) -> float:
        return max(0.0, self._penalty_until - time.monotonic())


def retry_after_seconds(headers) -> float:
    """Read a Retry-After header, falling back to a jittered default."""
    raw = None
    try:
        raw = headers.get("retry-after")
    except AttributeError:
        pass
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return 20.0 + random.uniform(0, 10)
