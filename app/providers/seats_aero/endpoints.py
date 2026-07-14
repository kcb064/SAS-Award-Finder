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

# seats.aero mileage-program identifier for SAS EuroBonus.
SOURCE_EUROBONUS = "eurobonus"


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
