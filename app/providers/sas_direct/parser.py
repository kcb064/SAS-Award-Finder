"""Parse SAS award-finder BFF JSON into domain objects. Second of two files that know SAS.

The BFF returns a JSON array of destination objects. The origin is NOT in the payload (it's a
query param), so it's passed in. Each destination carries an `availability.outbound` and
`availability.inbound` array; `inbound` is only populated for destination-scoped (route) requests.
Each date entry looks like:
    {"key":261102,"date":"2026-11-02","availableSeatsTotal":20,"AG":10,"AP":8,"AB":2}
Cabin keys (AG/AP/AB) are present only when seats > 0 in that cabin.
"""
from __future__ import annotations

import json
from typing import Any

from app.models import CABINS, DIRECTIONS, AwardFlight, DestinationInfo, ParsedFeed
from app.providers.base import FeedParseError  # re-exported: existing imports resolve here

__all__ = ["FeedParseError", "parse_feed"]


def _coerce(raw: Any) -> list[dict]:
    if isinstance(raw, (str, bytes, bytearray)):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        raise FeedParseError(f"expected a JSON array of destinations, got {type(raw).__name__}")
    return raw


def _destination_info(obj: dict) -> DestinationInfo:
    classes = obj.get("flightClasses") or []
    return DestinationInfo(
        code=str(obj["airportCode"]).upper(),
        city_name=obj.get("cityName"),
        country_name=obj.get("countryName"),
        city_code=obj.get("cityCode"),
        lat=obj.get("lat"),
        lng=obj.get("long"),  # BFF uses "long" for longitude
        flight_classes=tuple(classes),
        image=obj.get("image"),
    )


def _flights_for_direction(
    origin: str, destination: str, direction: str, entries: list[dict]
) -> list[AwardFlight]:
    out: list[AwardFlight] = []
    for entry in entries:
        date = entry.get("date")
        if not date:
            continue
        seats_total = int(entry.get("availableSeatsTotal", 0) or 0)
        for cabin in CABINS:
            if cabin not in entry:
                continue
            seats = int(entry[cabin] or 0)
            if seats <= 0:
                continue
            out.append(
                AwardFlight(
                    origin=origin,
                    destination=destination,
                    direction=direction,
                    flight_date=date,
                    cabin=cabin,
                    seats=seats,
                    seats_total=seats_total,
                    is_sas_operated=True,  # this feed is SAS-operated metal only
                )
            )
    return out


def parse_feed(raw: Any, origin: str) -> ParsedFeed:
    """Turn a BFF response into a `ParsedFeed` (catalog + flattened per-cabin observations).

    Works for both the network feed (inbound arrays empty) and the route feed (both populated).
    """
    origin = origin.upper()
    objs = _coerce(raw)
    destinations: list[DestinationInfo] = []
    flights: list[AwardFlight] = []
    for obj in objs:
        if "airportCode" not in obj:
            raise FeedParseError("destination object missing 'airportCode'")
        info = _destination_info(obj)
        destinations.append(info)
        availability = obj.get("availability") or {}
        for direction in DIRECTIONS:
            entries = availability.get(direction) or []
            flights.extend(_flights_for_direction(origin, info.code, direction, entries))
    return ParsedFeed(destinations=destinations, flights=flights)
