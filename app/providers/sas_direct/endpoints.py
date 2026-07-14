"""URL/param construction for the SAS award-finder BFF. One of exactly two files that know SAS.

Discovered in Phase 0/1 (see docs/api-notes.md):
- `destinations=` (empty)  -> NETWORK feed: every destination, outbound-only.
- `destinations=XXX`       -> ROUTE feed: one destination, BOTH outbound and inbound.
The `inbound` array is unlocked by destination-scoping, not by the month filter.
"""
from __future__ import annotations

from urllib.parse import urlencode

BASE_URL = "https://www.flysas.com"
BFF_PATH = "/bff/award-finder/destinations/v1"

# The award-finder page whose warmed context we fetch from (and a good Referer).
WARM_PAGE = f"{BASE_URL}/en/award-finder/"


def _params(origin: str, destinations: str, *, passengers: int, selected_month: str) -> dict[str, str]:
    return {
        "market": "en",
        "origin": origin.upper(),
        "destinations": destinations.upper(),
        "selectedMonth": selected_month,
        "passengers": str(passengers),
        "direct": "false",
        "availability": "true",
        "selectedFlightClass": "",
    }


def network_path(origin: str, *, passengers: int = 1, selected_month: str = "") -> str:
    """Relative path (for an in-page fetch) for the outbound-only all-destinations feed."""
    qs = urlencode(_params(origin, "", passengers=passengers, selected_month=selected_month))
    return f"{BFF_PATH}?{qs}"


def route_path(origin: str, destination: str, *, passengers: int = 1, selected_month: str = "") -> str:
    """Relative path for a single destination — returns both outbound and inbound arrays."""
    qs = urlencode(_params(origin, destination, passengers=passengers, selected_month=selected_month))
    return f"{BFF_PATH}?{qs}"


def booking_url(
    origin: str, destination: str, outbound_date: str, inbound_date: str | None = None
) -> str:
    """Human deep link into the flysas.com pay-with-points booking flow (used in alert bodies).

    Mirrors the URL the site itself builds (observed in Phase 0): `payWithPoints` is a bare flag.
    This link is for the human clicking the alert — the app never fetches it (that flow sits
    behind the hard Cloudflare challenge, see docs/api-notes.md).
    """
    params = {
        "payWithPoints": "",
        "tripType": "RT" if inbound_date else "OW",
        "origin": origin.upper(),
        "destination": destination.upper(),
        "outboundDate": outbound_date,
    }
    if inbound_date:
        params["inboundDate"] = inbound_date
    return f"{BASE_URL}/en?{urlencode(params)}"
