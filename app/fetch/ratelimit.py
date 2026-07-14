"""Single-flight, jittered rate limiter for SAS requests.

Used as an async context manager: it serializes callers (only one SAS request in flight at a time)
and enforces a randomized minimum gap after each request, so traffic from the home IP looks like a
human poking the site, not a scraper.
"""
from __future__ import annotations

import asyncio
import random
import time


class RateLimiter:
    def __init__(self, min_interval_s: float, max_interval_s: float) -> None:
        self.min_interval_s = float(min_interval_s)
        self.max_interval_s = max(float(max_interval_s), self.min_interval_s)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def __aenter__(self) -> "RateLimiter":
        await self._lock.acquire()
        now = time.monotonic()
        if now < self._next_allowed:
            await asyncio.sleep(self._next_allowed - now)
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Schedule the next permitted request a jittered interval from now (after this one finished).
        self._next_allowed = time.monotonic() + random.uniform(
            self.min_interval_s, self.max_interval_s
        )
        self._lock.release()
