"""Per-leg diffing: new sweep vs the `award_current` anchor. Pure functions.

Compares the flights parsed from a fresh route sweep against the previously known state for the
same scope and classifies changes:
- key present now but not before          -> opened
- key present before but missing now      -> closed (ONLY when the sweep is fully 'ok' —
  partial/failed sweeps never emit negative diffs, preventing false "award gone" alarms)
- seats went <2 -> >=2 on SAS-operated    -> voucher_pair (a 2-for-1 candidate leg appeared)

The caller is responsible for scoping `previous` to exactly the flights the sweep covers (for a
route sweep: both directions of one origin->destination), otherwise unrelated keys would read as
closed. Trip-level (round-trip) alerting is layered on top in services/watches.py; this module
knows nothing about watches.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from app.models import AwardFlight

VOUCHER_MIN_SEATS = 2


@dataclass(frozen=True, slots=True)
class CurrentLeg:
    """The previously known state of one flight_key (a row from `award_current`)."""

    flight_key: str
    origin: str
    destination: str
    direction: str
    flight_date: str
    cabin: str
    seats: int
    is_sas_operated: bool = True

    @classmethod
    def from_row(cls, row: Mapping) -> "CurrentLeg":
        return cls(
            flight_key=row["flight_key"], origin=row["origin"], destination=row["destination"],
            direction=row["direction"], flight_date=row["flight_date"], cabin=row["cabin"],
            seats=row["seats"], is_sas_operated=bool(row["is_sas_operated"]),
        )


@dataclass(slots=True)
class LegDiff:
    """Classified changes from one sweep. `baseline` marks a first-ever observation of the scope
    (nothing previously known) — callers use it to suppress an opened-alert storm on first run."""

    opened: list[AwardFlight] = field(default_factory=list)
    closed: list[CurrentLeg] = field(default_factory=list)
    voucher_pairs: list[AwardFlight] = field(default_factory=list)
    baseline: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(self.opened or self.closed or self.voucher_pairs)


def diff_legs(
    previous: Iterable[CurrentLeg],
    new_flights: Iterable[AwardFlight],
    *,
    sweep_ok: bool,
) -> LegDiff:
    """Classify per-leg changes between the previous known state and a new sweep's flights."""
    prev_by_key = {leg.flight_key: leg for leg in previous}
    diff = LegDiff(baseline=not prev_by_key)

    seen: set[str] = set()
    for f in new_flights:
        key = f.key
        seen.add(key)
        prev = prev_by_key.get(key)
        if prev is None:
            diff.opened.append(f)
            # A leg that opens straight into >=2 seats is itself a fresh voucher candidate.
            if f.is_sas_operated and f.seats >= VOUCHER_MIN_SEATS:
                diff.voucher_pairs.append(f)
        elif (
            f.is_sas_operated
            and prev.seats < VOUCHER_MIN_SEATS
            and f.seats >= VOUCHER_MIN_SEATS
        ):
            diff.voucher_pairs.append(f)

    if sweep_ok:
        diff.closed = [leg for key, leg in prev_by_key.items() if key not in seen]
    return diff
