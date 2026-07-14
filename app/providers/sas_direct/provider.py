"""SASDirectProvider — the primary provider. Ties the browser fetcher, rate limiter, daily budget,
and parser together. All SAS-specific knowledge is confined to this package (endpoints + parser).
"""
from __future__ import annotations

from app.fetch.budget import Budget, ProviderCall
from app.fetch.engine import BrowserFetcher
from app.fetch.ratelimit import RateLimiter
from app.models import ParsedFeed
from app.providers.base import (
    CAP_NETWORK,
    CAP_ROUTE,
    SCOPE_NETWORK,
    SCOPE_ROUTE,
    ProviderFetch,
)
from app.providers.sas_direct import endpoints
from app.providers.sas_direct.parser import parse_feed


class SASDirectProvider:
    name = "sas_direct"
    capabilities = {CAP_NETWORK, CAP_ROUTE}

    def __init__(
        self,
        fetcher: BrowserFetcher,
        rate_limiter: RateLimiter,
        budget: Budget,
        *,
        passengers: int = 1,
    ) -> None:
        self._fetcher = fetcher
        self._rate = rate_limiter
        self._budget = budget
        self._passengers = passengers

    async def fetch(
        self, scope: str, origin: str, destination: str | None = None
    ) -> ProviderFetch:
        origin = origin.upper()
        if scope == SCOPE_NETWORK:
            path = endpoints.network_path(origin, passengers=self._passengers)
            dest = None
        elif scope == SCOPE_ROUTE:
            if not destination:
                raise ValueError("route scope requires a destination")
            dest = destination.upper()
            path = endpoints.route_path(origin, dest, passengers=self._passengers)
        else:
            raise ValueError(f"unknown scope: {scope!r}")

        self._budget.check()
        http_status: int | None = None
        byte_size = 0
        duration_ms = 0
        try:
            async with self._rate:
                result = await self._fetcher.fetch_json(path)
            http_status = result.status
            byte_size = result.byte_size
            duration_ms = result.duration_ms
            feed = parse_feed(result.text, origin)
        except Exception:
            # Record the failed attempt against the budget/audit (a block still counts as a request)
            # before propagating — the caller decides whether to alert or degrade.
            self._budget.record(
                ProviderCall(scope, origin, dest, "failed", http_status, byte_size, duration_ms)
            )
            raise

        self._budget.record(
            ProviderCall(scope, origin, dest, "ok", http_status, byte_size, duration_ms)
        )
        return ProviderFetch(
            scope=scope,
            origin=origin,
            destination=dest,
            feed=feed,
            raw_text=result.text,
            http_status=result.status,
            byte_size=result.byte_size,
            duration_ms=result.duration_ms,
            status="ok",
        )

    async def fetch_network(self, origin: str) -> ParsedFeed:
        return (await self.fetch(SCOPE_NETWORK, origin)).feed

    async def fetch_route(self, origin: str, destination: str) -> ParsedFeed:
        return (await self.fetch(SCOPE_ROUTE, origin, destination)).feed
