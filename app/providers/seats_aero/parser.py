"""Parse seats.aero Partner API cached-search JSON into domain objects. Second of two files that
know seats.aero.

Each `data` entry is one route+date with per-cabin fields keyed by fare-class letter:
    {"Route": {"OriginAirport": "CPH", "DestinationAirport": "BOS", "Source": "eurobonus"},
     "Date": "2026-11-02",
     "YAvailable": true, "YRemainingSeats": 5, "YAirlines": "SK",
     "JAvailable": true, "JRemainingSeats": 0, "JAirlines": "SK, KL", ...}

Mapping decisions:
- Cabins: Y->AG (Economy), W->AP (Premium), J->AB (Business). F is skipped — SAS sells no
  EuroBonus First awards, so an F entry can't be booked with Kevin's points.
- `RemainingSeats == 0` while `Available` is true means seats.aero doesn't know the count; we
  record 1 seat ("at least one") so the trip surfaces without inflating voucher-pair logic,
  which needs >=2 confirmed seats.
- `is_sas_operated` is true only when the cabin's airline list is exactly SK. Unknown or partner
  metal stays false: the 2-for-1 voucher and `sas_only` watches must never fire on a guess.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.models import AwardFlight, DestinationInfo, ParsedFeed
from app.providers.base import FeedParseError
from app.providers.seats_aero.endpoints import SKYTEAM_SOURCES, SOURCE_EUROBONUS

# seats.aero fare-class letter -> SAS cabin code.
CABIN_MAP = {"Y": "AG", "W": "AP", "J": "AB"}


def _coerce(raw: Any) -> list[dict]:
    if isinstance(raw, (str, bytes, bytearray)):
        raw = json.loads(raw)
    if isinstance(raw, dict):
        raw = raw.get("data")
    if not isinstance(raw, list):
        raise FeedParseError(
            f"expected a seats.aero response with a 'data' array, got {type(raw).__name__}"
        )
    return raw


def _sas_operated(airlines: str | None) -> bool:
    tokens = [t.strip().upper() for t in (airlines or "").split(",") if t.strip()]
    return bool(tokens) and all(t == "SK" for t in tokens)


def _entry_flights(
    entry: dict, route_origin: str, route_destination: str, direction: str
) -> list[AwardFlight]:
    date = entry.get("Date")
    if not date:
        raise FeedParseError("seats.aero entry missing 'Date'")
    per_cabin: list[tuple[str, int, bool]] = []
    for letter, cabin in CABIN_MAP.items():
        if not entry.get(f"{letter}Available"):
            continue
        seats = int(entry.get(f"{letter}RemainingSeats") or 0) or 1
        per_cabin.append((cabin, seats, _sas_operated(entry.get(f"{letter}Airlines"))))
    seats_total = sum(s for _, s, _ in per_cabin)
    return [
        AwardFlight(
            origin=route_origin, destination=route_destination, direction=direction,
            flight_date=date, cabin=cabin, seats=seats, seats_total=seats_total,
            is_sas_operated=sas,
        )
        for cabin, seats, sas in per_cabin
    ]


def parse_search(
    raw: Any, origin: str, destination: str | None = None, *, source: str = SOURCE_EUROBONUS
) -> ParsedFeed:
    """Turn cached-search entries into a `ParsedFeed` matching the SAS feed's conventions.

    Route scope (destination given): entries origin->destination become `outbound`, entries
    destination->origin become `inbound` — origin/destination on every AwardFlight name the
    ROUTE as searched, not the leg, exactly like the SAS parser. Network scope keeps only
    outbound entries from `origin`. Entries from other mileage programs are dropped.
    """
    origin = origin.upper()
    destination = destination.upper() if destination else None
    flights: list[AwardFlight] = []
    dest_codes: list[str] = []
    seen: set[str] = set()
    for entry in _coerce(raw):
        route = entry.get("Route")
        if not isinstance(route, dict):
            raise FeedParseError("seats.aero entry missing 'Route'")
        if source and (route.get("Source") or "").lower() != source:
            continue
        o = str(route.get("OriginAirport") or "").upper()
        d = str(route.get("DestinationAirport") or "").upper()
        if not o or not d:
            raise FeedParseError("seats.aero Route missing airport codes")
        if destination is None:
            if o != origin:
                continue
            flights.extend(_entry_flights(entry, origin, d, "outbound"))
            target = d
        elif o == origin and d == destination:
            flights.extend(_entry_flights(entry, origin, destination, "outbound"))
            target = d
        elif o == destination and d == origin:
            flights.extend(_entry_flights(entry, origin, destination, "inbound"))
            target = destination
        else:
            continue
        if target not in seen:
            seen.add(target)
            dest_codes.append(target)
    # Codes only — seats.aero has no city/country metadata. The airports upsert keeps whatever
    # names an earlier SAS catalog already filled in.
    destinations = [DestinationInfo(code=c) for c in dest_codes if c != origin]
    return ParsedFeed(destinations=destinations, flights=flights)


# ---- SkyTeam tab (display-only, never persisted) --------------------------------------------


@dataclass(frozen=True, slots=True)
class SkyTeamRow:
    """One date+cabin availability row for the SkyTeam tab.

    Unlike AwardFlight, this keeps partner airlines, the source program's own mileage cost, and
    the direct flag — and `seats` stays 0 when the count is unknown (the UI shows "1+"),
    because the voucher-usable badge must only fire on >=2 CONFIRMED seats.

    LIVE-VERIFIED field semantics (2026-07-24): `{L}MileageCost` is a string, "0" == no figure;
    `{L}TotalTaxes` is an int in MINOR units of TaxesCurrency (USD cents, DKK øre) — converted
    to major units here; the non-Raw fields are seats.aero's "reasonably priced" view (the Raw
    variants include dynamic pricing) and are the ones we read.
    """

    date: str
    origin: str
    destination: str
    cabin: str                      # AG/AP/AB via CABIN_MAP
    airlines: tuple[str, ...]       # e.g. ("AF", "KL", "SK")
    seats: int                      # RemainingSeats; 0 == count unknown ("at least 1")
    sas_operated: bool
    direct: bool | None             # None when the API omits the flag
    mileage_cost: int | None        # in the SOURCE program's miles, not EuroBonus points
    total_taxes: float | None       # major currency units
    taxes_currency: str | None
    source: str                     # mileage program the row came from ("flyingblue", ...)


def _mileage(value: Any) -> int | None:
    try:
        cost = int(value)
    except (TypeError, ValueError):
        return None
    return cost or None             # "0" means "no figure", not free

def _taxes(value: Any) -> float | None:
    try:
        minor = float(value)
    except (TypeError, ValueError):
        return None
    return minor / 100.0 if minor > 0 else None


def parse_partner_rows(
    raw: Any, *, sources: tuple[str, ...] = SKYTEAM_SOURCES
) -> list[SkyTeamRow]:
    """Cached-search entries -> display rows, keeping ALL airlines (partner metal included).

    No origin/destination filtering — the request already scoped the search. Entries from
    programs outside `sources` are dropped; each kept row records which program priced it.
    """
    allowed = {s.lower() for s in sources}
    rows: list[SkyTeamRow] = []
    for entry in _coerce(raw):
        route = entry.get("Route")
        if not isinstance(route, dict):
            raise FeedParseError("seats.aero entry missing 'Route'")
        source = (route.get("Source") or "").lower()
        if source not in allowed:
            continue
        origin = str(route.get("OriginAirport") or "").upper()
        dest = str(route.get("DestinationAirport") or "").upper()
        date = entry.get("Date")
        if not origin or not dest:
            raise FeedParseError("seats.aero Route missing airport codes")
        if not date:
            raise FeedParseError("seats.aero entry missing 'Date'")
        for letter, cabin in CABIN_MAP.items():
            if not entry.get(f"{letter}Available"):
                continue
            airlines_raw = entry.get(f"{letter}Airlines")
            airlines = tuple(
                t.strip().upper() for t in (airlines_raw or "").split(",") if t.strip()
            )
            direct = entry.get(f"{letter}Direct")
            rows.append(SkyTeamRow(
                date=date, origin=origin, destination=dest, cabin=cabin,
                airlines=airlines,
                seats=int(entry.get(f"{letter}RemainingSeats") or 0),
                sas_operated=_sas_operated(airlines_raw),
                direct=bool(direct) if direct is not None else None,
                mileage_cost=_mileage(entry.get(f"{letter}MileageCost")),
                total_taxes=_taxes(entry.get(f"{letter}TotalTaxes")),
                taxes_currency=(entry.get("TaxesCurrency") or None),
                source=source,
            ))
    return rows
