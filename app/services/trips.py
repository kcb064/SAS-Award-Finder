"""Round-trip pairing: turn per-leg `AwardFlight` observations into bookable `TripOption`s.

A round-trip is bookable only if BOTH legs qualify — an outbound date in the outbound window with
>= min_seats in the cabin, paired with an inbound date in the return window such that
min_stay <= (inbound - outbound) days <= max_stay and inbound also has >= min_seats. One-way options
are just qualifying outbound legs. Pricing/voucher value is layered on later (see services/value.py).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable

from app.models import CABINS, AwardFlight, TripOption


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _in_window(day: date, start: str | None, end: str | None) -> bool:
    if start and day < _d(start):
        return False
    if end and day > _d(end):
        return False
    return True


def _by_cabin_date(
    flights: Iterable[AwardFlight], direction: str
) -> dict[str, dict[str, AwardFlight]]:
    """Index qualifying-direction flights as cabin -> {date -> flight}."""
    out: dict[str, dict[str, AwardFlight]] = defaultdict(dict)
    for f in flights:
        if f.direction == direction:
            out[f.cabin][f.flight_date] = f
    return out


def one_way_options(
    flights: Iterable[AwardFlight],
    *,
    cabin: str | None = None,
    out_from: str | None = None,
    out_to: str | None = None,
    min_seats: int = 1,
) -> list[TripOption]:
    """Qualifying outbound legs as one-way `TripOption`s, sorted by (date, cabin)."""
    flights = list(flights)
    cabins = (cabin,) if cabin else CABINS
    outbound = _by_cabin_date(flights, "outbound")
    options: list[TripOption] = []
    for cab in cabins:
        for day_str, f in outbound.get(cab, {}).items():
            if f.seats < min_seats or not _in_window(_d(day_str), out_from, out_to):
                continue
            options.append(
                TripOption(
                    origin=f.origin, destination=f.destination, cabin=cab,
                    outbound_date=day_str, out_seats=f.seats, out_sas_operated=f.is_sas_operated,
                )
            )
    options.sort(key=lambda t: (t.outbound_date, t.cabin))
    return options


def pair_round_trips(
    flights: Iterable[AwardFlight],
    *,
    cabin: str | None = None,
    out_from: str | None = None,
    out_to: str | None = None,
    ret_from: str | None = None,
    ret_to: str | None = None,
    min_stay_days: int = 2,
    max_stay_days: int = 30,
    min_seats: int = 1,
) -> list[TripOption]:
    """All bookable round-trips, sorted by (outbound_date, inbound_date, cabin).

    Both legs must have >= min_seats in the cabin, and the stay length (inbound - outbound) must fall
    within [min_stay_days, max_stay_days]. Returns an empty list if no return exists in the window.
    """
    flights = list(flights)
    cabins = (cabin,) if cabin else CABINS
    outbound = _by_cabin_date(flights, "outbound")
    inbound = _by_cabin_date(flights, "inbound")

    options: list[TripOption] = []
    for cab in cabins:
        out_days = sorted(
            (ds, f) for ds, f in outbound.get(cab, {}).items()
            if f.seats >= min_seats and _in_window(_d(ds), out_from, out_to)
        )
        in_days = sorted(
            (ds, f) for ds, f in inbound.get(cab, {}).items()
            if f.seats >= min_seats and _in_window(_d(ds), ret_from, ret_to)
        )
        for out_str, of in out_days:
            od = _d(out_str)
            for in_str, inf in in_days:
                stay = (_d(in_str) - od).days
                if stay < min_stay_days or stay > max_stay_days:
                    continue
                options.append(
                    TripOption(
                        origin=of.origin, destination=of.destination, cabin=cab,
                        outbound_date=out_str, out_seats=of.seats,
                        inbound_date=in_str, in_seats=inf.seats, stay_days=stay,
                        out_sas_operated=of.is_sas_operated, in_sas_operated=inf.is_sas_operated,
                    )
                )
    options.sort(key=lambda t: (t.outbound_date, t.inbound_date or "", t.cabin))
    return options


def best_round_trip_per_outbound(options: list[TripOption]) -> list[TripOption]:
    """Collapse to the single best return per (outbound_date, cabin): the most seats, then shortest
    stay. Useful for a less noisy Search view when a window yields many return dates."""
    best: dict[tuple[str, str], TripOption] = {}
    for opt in options:
        key = (opt.outbound_date, opt.cabin)
        cur = best.get(key)
        if cur is None or _better(opt, cur):
            best[key] = opt
    return sorted(best.values(), key=lambda t: (t.outbound_date, t.cabin))


def _better(a: TripOption, b: TripOption) -> bool:
    a_seats = min(a.out_seats, a.in_seats or a.out_seats)
    b_seats = min(b.out_seats, b.in_seats or b.out_seats)
    if a_seats != b_seats:
        return a_seats > b_seats
    return (a.stay_days or 0) < (b.stay_days or 0)
