"""Provider abstraction — the pivot insurance.

An `AwardProvider` produces availability for a whole origin network (outbound-only) or for a single
route (both directions). Higher-level behavior — round-trip pairing, diffing, value math — lives in
`services/`, not here, so a second provider (seats.aero, Phase 4) can implement the same primitives
and everything above keeps working.

`fetch()` is the unit of work: it returns the parsed feed *plus* the raw payload and call metadata,
so the snapshot store can persist raw JSON for re-parsing after a format change. `fetch_network` /
`fetch_route` are thin convenience wrappers over it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.models import ParsedFeed

# Scopes.
SCOPE_NETWORK = "network"        # all destinations, outbound-only
SCOPE_ROUTE = "route"            # one destination, both directions

# Capability flags a provider may advertise.
CAP_NETWORK = "network"
CAP_ROUTE = "route"
CAP_CASH_FARE = "cash_fare"      # reserved: no provider supplies cash fares yet


class FeedParseError(ValueError):
    """A provider payload isn't the shape its parser expects — surfaced as an ops alert, never
    as a silent 'no availability'. Shared across providers so callers can catch one type."""


@dataclass(slots=True)
class ProviderFetch:
    """One fetch's full result: parsed feed + raw payload + metadata for persistence/audit."""

    scope: str
    origin: str
    destination: str | None
    feed: ParsedFeed
    raw_text: str
    http_status: int
    byte_size: int
    duration_ms: int
    status: str = "ok"           # 'ok' | 'partial' | 'failed'


@runtime_checkable
class AwardProvider(Protocol):
    name: str
    capabilities: set[str]

    async def fetch(self, scope: str, origin: str, destination: str | None = None) -> ProviderFetch:
        """Fetch one scope and return parsed feed + raw payload + metadata."""
        ...

    async def fetch_network(self, origin: str) -> ParsedFeed:
        """Outbound-only availability for every destination from `origin`."""
        ...

    async def fetch_route(self, origin: str, destination: str) -> ParsedFeed:
        """Both outbound and inbound availability for one `origin`->`destination` route."""
        ...
