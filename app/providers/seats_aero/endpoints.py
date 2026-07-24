"""URL/param construction for the seats.aero Partner API. One of exactly two files that know
seats.aero.

Written against the published Partner API docs (https://developers.seats.aero) but NOT yet
verified against a live key — Kevin hasn't subscribed. If a request shape is off when the key
arrives, this file (plus parser.py) is the whole blast radius.

The cached-search endpoint accepts comma-separated airport lists, which maps neatly onto our two
scopes:
- ROUTE:   origin_airport=CPH,BOS & destination_airport=CPH,BOS -> one call returns BOTH
           directions (CPH->BOS and BOS->CPH entries; the parser splits them by Route).
- NETWORK: origin_airport=CPH with no destination filter -> outbound-only, every cached
           destination for the program.
Results are paginated by `cursor`/`hasMore`; auth is a `Partner-Authorization` header.
"""
from __future__ import annotations

BASE_URL = "https://seats.aero/partnerapi"
SEARCH_PATH = "/search"

# seats.aero mileage-program identifier for SAS EuroBonus. LIVE-VERIFIED 2026-07-24: this source
# does NOT exist on seats.aero — no entry ever carries it, so the AF_PROVIDER=seats_aero fallback
# yields empty feeds. Kept for the legacy parse path; see docs/api-notes.md Phase 5.
SOURCE_EUROBONUS = "eurobonus"

# SkyTeam programs seats.aero actually indexes (live-verified). Their availability is the shared
# SkyTeam partner space EuroBonus can book too — but the mileage prices are THEIRS, not EuroBonus.
SKYTEAM_SOURCES = ("flyingblue", "delta", "virginatlantic")


def _base_params(*, start_date: str, end_date: str, take: int, cursor: str | None) -> dict[str, str]:
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "take": str(take),
        "include_trips": "false",
        "only_direct_flights": "false",
    }
    if cursor:
        params["cursor"] = str(cursor)
    return params


def network_params(
    origin: str, *, start_date: str, end_date: str, take: int = 1000, cursor: str | None = None
) -> dict[str, str]:
    """Cached search across every destination from `origin` (outbound-only by construction)."""
    return {"origin_airport": origin.upper(), **_base_params(
        start_date=start_date, end_date=end_date, take=take, cursor=cursor)}


def route_params(
    origin: str,
    destination: str,
    *,
    start_date: str,
    end_date: str,
    take: int = 1000,
    cursor: str | None = None,
) -> dict[str, str]:
    """Cached search covering one route in BOTH directions via comma lists (single call)."""
    pair = f"{origin.upper()},{destination.upper()}"
    return {"origin_airport": pair, "destination_airport": pair, **_base_params(
        start_date=start_date, end_date=end_date, take=take, cursor=cursor)}


def search_params(
    origins: list[str],
    destinations: list[str] | None,
    *,
    start_date: str,
    end_date: str,
    take: int = 1000,
    cursor: str | None = None,
) -> dict[str, str]:
    """Free-form cached search: any origin list, destination list (SkyTeam tab).

    LIVE-VERIFIED: /search silently returns ZERO entries without `destination_airport` —
    callers must always send destinations. Keep the list to ~30 codes; the comma list goes in
    the query string, and huge URLs risk 414s / undocumented API limits.
    """
    params = {"origin_airport": ",".join(o.upper() for o in origins), **_base_params(
        start_date=start_date, end_date=end_date, take=take, cursor=cursor)}
    if destinations:
        params["destination_airport"] = ",".join(d.upper() for d in destinations)
    return params
