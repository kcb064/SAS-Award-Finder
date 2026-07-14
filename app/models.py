"""Plain domain dataclasses shared across the parser, services, and web layers.

These are deliberately not SQLAlchemy models — the DB layer is raw SQL. These types are the
in-memory contract: the parser produces `AwardFlight`s, `services/trips.py` produces `TripOption`s,
the web layer renders them.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# Cabin codes as SAS returns them, plus display names.
CABINS = ("AG", "AP", "AB")
CABIN_NAMES = {"AG": "Economy", "AP": "Premium Economy", "AB": "Business"}
DIRECTIONS = ("outbound", "inbound")


def flight_key(direction: str, date: str, origin: str, destination: str, cabin: str) -> str:
    """Stable identity for one date+cabin+direction on one route. Anchors diffing across sweeps."""
    raw = f"{direction}|{date}|{origin}|{destination}|{cabin}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AwardFlight:
    """One observation: N award seats in one cabin, on one date, one direction of one route."""

    origin: str
    destination: str
    direction: str            # 'outbound' | 'inbound'
    flight_date: str          # YYYY-MM-DD
    cabin: str                # 'AG' | 'AP' | 'AB'
    seats: int                # seats in this cabin
    seats_total: int          # availableSeatsTotal across cabins for the date
    is_sas_operated: bool = True

    @property
    def key(self) -> str:
        return flight_key(self.direction, self.flight_date, self.origin, self.destination, self.cabin)


@dataclass(frozen=True, slots=True)
class DestinationInfo:
    """Catalog metadata for a destination, from the network feed."""

    code: str
    city_name: str | None = None
    country_name: str | None = None
    city_code: str | None = None
    lat: float | None = None
    lng: float | None = None
    flight_classes: tuple[str, ...] = ()
    image: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    """Result of parsing one BFF response: catalog + flattened per-cabin observations."""

    destinations: list[DestinationInfo] = field(default_factory=list)
    flights: list[AwardFlight] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TripOption:
    """A concrete bookable trip: a round-trip (both dates set) or a one-way (inbound_date None)."""

    origin: str
    destination: str
    cabin: str
    outbound_date: str
    out_seats: int
    inbound_date: str | None = None
    in_seats: int | None = None
    stay_days: int | None = None
    points_total: int | None = None
    taxes_total: float | None = None
    taxes_currency: str = "USD"
    out_sas_operated: bool = True
    in_sas_operated: bool = True
    voucher_eligible: bool = False

    @property
    def is_round_trip(self) -> bool:
        return self.inbound_date is not None
