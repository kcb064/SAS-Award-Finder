"""SeatsAeroProvider — the fallback provider, selected with AF_PROVIDER=seats_aero.

Same shape as SASDirectProvider: rate limiter + shared daily budget around every HTTP call, one
`ProviderFetch` out. Differences: plain httpx against an authenticated API (no browser), and
responses are cursor-paginated — one fetch() may spend several budgeted calls; the merged pages
are stored as one snapshot whose raw text round-trips through the same parser.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta

import httpx

from app.fetch.budget import Budget, ProviderCall
from app.fetch.ratelimit import RateLimiter
from app.models import ParsedFeed
from app.providers.base import (
    CAP_NETWORK,
    CAP_ROUTE,
    SCOPE_NETWORK,
    SCOPE_ROUTE,
    ProviderFetch,
)
from app.providers.seats_aero import endpoints
from app.providers.seats_aero.parser import parse_search

# How far ahead to ask for availability. Mirrors the SAS feed's ~1-year horizon.
HORIZON_DAYS = 353


class SeatsAeroProvider:
    name = "seats_aero"
    capabilities = {CAP_NETWORK, CAP_ROUTE}

    def __init__(
        self,
        api_key: str,
        rate_limiter: RateLimiter,
        budget: Budget,
        *,
        base_url: str = endpoints.BASE_URL,
        source: str = endpoints.SOURCE_EUROBONUS,
        page_size: int = 1000,
        max_pages: int = 10,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("seats.aero provider needs an API key (AF_SEATS_AERO_API_KEY)")
        self._api_key = api_key
        self._rate = rate_limiter
        self._budget = budget
        self._base_url = base_url
        self._source = source
        self._page_size = page_size
        self._max_pages = max_pages
        self._client = client            # injectable for tests; lazily built otherwise

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)
        return self._client

    @property
    def _headers(self) -> dict[str, str]:
        # Sent per-request (not baked into the client) so injected test clients are covered too.
        return {"Partner-Authorization": self._api_key, "Accept": "application/json"}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(
        self, scope: str, origin: str, destination: str | None = None
    ) -> ProviderFetch:
        origin = origin.upper()
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=HORIZON_DAYS)).isoformat()
        if scope == SCOPE_NETWORK:
            dest = None
            params = endpoints.network_params(
                origin, start_date=start, end_date=end, take=self._page_size)
        elif scope == SCOPE_ROUTE:
            if not destination:
                raise ValueError("route scope requires a destination")
            dest = destination.upper()
            params = endpoints.route_params(
                origin, dest, start_date=start, end_date=end, take=self._page_size)
        else:
            raise ValueError(f"unknown scope: {scope!r}")

        entries: list[dict] = []
        http_status: int | None = None
        byte_size = 0
        duration_ms = 0
        cursor: str | None = None
        for _ in range(self._max_pages):
            self._budget.check()
            page = dict(params)
            if cursor:
                page["cursor"] = str(cursor)
            try:
                async with self._rate:
                    t0 = time.perf_counter()
                    resp = await self._http().get(
                        endpoints.SEARCH_PATH, params=page, headers=self._headers,
                    )
                    duration_ms += int((time.perf_counter() - t0) * 1000)
                http_status = resp.status_code
                byte_size += len(resp.content)
                resp.raise_for_status()
                payload = resp.json()
            except Exception:
                self._budget.record(ProviderCall(
                    scope, origin, dest, "failed", http_status, byte_size, duration_ms,
                    provider=self.name,
                ))
                raise
            self._budget.record(ProviderCall(
                scope, origin, dest, "ok", http_status, byte_size, duration_ms,
                provider=self.name,
            ))
            entries.extend(payload.get("data") or [])
            cursor = payload.get("cursor")
            if not payload.get("hasMore") or not cursor:
                break

        feed = parse_search({"data": entries}, origin, dest, source=self._source)
        return ProviderFetch(
            scope=scope,
            origin=origin,
            destination=dest,
            feed=feed,
            raw_text=json.dumps({"data": entries}),
            http_status=http_status or 0,
            byte_size=byte_size,
            duration_ms=duration_ms,
            status="ok",
        )

    async def fetch_network(self, origin: str) -> ParsedFeed:
        return (await self.fetch(SCOPE_NETWORK, origin)).feed

    async def fetch_route(self, origin: str, destination: str) -> ParsedFeed:
        return (await self.fetch(SCOPE_ROUTE, origin, destination)).feed
