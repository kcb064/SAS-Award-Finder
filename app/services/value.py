"""Points/taxes pricing (from the seed zone table), 2-for-1 voucher logic, and cash value (cpp).

The award feed has no prices, so points and taxes come from `config/points_table.yaml`. The voucher
math implements the SAS Amex 2-for-1: on a SAS-operated round trip with >=2 award seats in the cabin
on BOTH legs, one award's points cover 2 passengers (taxes paid x2), so effective points per person
is points_total / 2.

Phase 4 adds the points-VALUE side: what the same trip costs in cash, and therefore what each point
is worth when redeemed (cpp, cents per point). Cash prices come from two tiers — a manual quote
Kevin enters after checking the real fare (the `cash_fares` table, authoritative), falling back to
the zone table's `cash_estimates` (rough, marked "est." in the UI). The booking-flow cash price
cannot be scraped (hard Cloudflare challenge, see docs/api-notes.md), so manual-plus-estimate is
deliberate, not a stopgap.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app import db
from app.models import TripOption


@dataclass(frozen=True, slots=True)
class LegPrice:
    points: int
    taxes: float


@dataclass(frozen=True, slots=True)
class TripPrice:
    points_total: int
    taxes_total: float
    voucher_eligible: bool
    # With the 2-for-1 voucher applied to an eligible round trip:
    points_per_person_voucher: int | None
    taxes_total_voucher: float | None


def _zone_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


class ZoneTable:
    """Loads the seed points/taxes table and resolves airports to zones and prices to legs/trips."""

    def __init__(self, data: dict) -> None:
        self._country_to_zone: dict[str, str] = {}
        for zone, countries in (data.get("zones") or {}).items():
            for country in countries:
                self._country_to_zone[country.strip().lower()] = zone
        self._airport_zones: dict[str, str] = {
            k.upper(): v for k, v in (data.get("airport_zones") or {}).items()
        }
        self._default_zone: str = data.get("default_zone", "EUROPE")
        self._points: dict[str, dict[str, int]] = data.get("points") or {}
        self._default_points: dict[str, int] = data.get("default_points") or {}
        self._taxes: dict[str, float] = data.get("taxes") or {}
        self._default_taxes: float = float(data.get("default_taxes", 60))
        self._dep_surcharge: dict[str, dict[str, int]] = {
            k.strip().lower(): v for k, v in (data.get("departure_country_surcharge") or {}).items()
        }
        self._cash: dict[str, dict[str, float]] = data.get("cash_estimates") or {}
        self._default_cash: dict[str, float] = data.get("default_cash") or {}

    @classmethod
    def load(cls, path: Path) -> "ZoneTable":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(data)

    # ---- zone resolution -------------------------------------------------------------

    def zone_for(self, airport_code: str | None, country_name: str | None) -> str:
        if airport_code and airport_code.upper() in self._airport_zones:
            return self._airport_zones[airport_code.upper()]
        if country_name:
            zone = self._country_to_zone.get(country_name.strip().lower())
            if zone:
                return zone
        return self._default_zone

    # ---- pricing ---------------------------------------------------------------------

    def _points_for(self, zone_a: str, zone_b: str, cabin: str) -> int:
        table = self._points.get(_zone_key(zone_a, zone_b), self._default_points)
        return int(table.get(cabin, self._default_points.get(cabin, 0)))

    def _base_taxes_for(self, zone_a: str, zone_b: str) -> float:
        return float(self._taxes.get(_zone_key(zone_a, zone_b), self._default_taxes))

    def leg_price(
        self,
        *,
        dep_code: str | None,
        dep_country: str | None,
        arr_code: str | None,
        arr_country: str | None,
        cabin: str,
    ) -> LegPrice:
        """Price one directional leg. Taxes include any departure-country surcharge (e.g. UK APD)."""
        dep_zone = self.zone_for(dep_code, dep_country)
        arr_zone = self.zone_for(arr_code, arr_country)
        points = self._points_for(dep_zone, arr_zone, cabin)
        taxes = self._base_taxes_for(dep_zone, arr_zone)
        surcharge = self._dep_surcharge.get((dep_country or "").strip().lower())
        if surcharge:
            taxes += float(surcharge.get(cabin, 0))
        return LegPrice(points=points, taxes=taxes)

    def rt_cash_estimate(
        self,
        *,
        origin_code: str | None,
        origin_country: str | None,
        dest_code: str | None,
        dest_country: str | None,
        cabin: str,
    ) -> float | None:
        """Estimated ROUND-TRIP cash price per person (USD) for a route+cabin, or None if the
        table has no figure at all. A rough display anchor for cpp — not a quote."""
        zone_a = self.zone_for(origin_code, origin_country)
        zone_b = self.zone_for(dest_code, dest_country)
        table = self._cash.get(_zone_key(zone_a, zone_b), self._default_cash)
        value = table.get(cabin, self._default_cash.get(cabin))
        return float(value) if value is not None else None


def voucher_eligible(trip: TripOption, min_seats: int = 2) -> bool:
    """The Amex 2-for-1 applies to a SAS-operated round trip with >=`min_seats` on BOTH legs.

    Missing seat counts are treated as not-yet-confirmed (amber at booking) — not eligible here.
    """
    if not trip.is_round_trip:
        return False
    if not (trip.out_sas_operated and trip.in_sas_operated):
        return False
    if trip.out_seats is None or trip.in_seats is None:
        return False
    return trip.out_seats >= min_seats and trip.in_seats >= min_seats


def price_trip(
    zones: ZoneTable,
    trip: TripOption,
    *,
    origin_country: str | None,
    dest_country: str | None,
) -> TripPrice:
    """Compute points_total, taxes_total, and voucher-adjusted figures for a trip.

    Outbound leg: origin -> destination. Inbound leg (if any): destination -> origin.
    """
    out = zones.leg_price(
        dep_code=trip.origin, dep_country=origin_country,
        arr_code=trip.destination, arr_country=dest_country, cabin=trip.cabin,
    )
    points_total = out.points
    taxes_total = out.taxes
    if trip.is_round_trip:
        back = zones.leg_price(
            dep_code=trip.destination, dep_country=dest_country,
            arr_code=trip.origin, arr_country=origin_country, cabin=trip.cabin,
        )
        points_total += back.points
        taxes_total += back.taxes

    eligible = voucher_eligible(trip)
    points_pp_voucher: int | None = None
    taxes_voucher: float | None = None
    if eligible:
        # One award covers 2 pax: same points_total, but per-person is halved; taxes paid per pax.
        points_pp_voucher = round(points_total / 2)
        taxes_voucher = taxes_total * 2

    return TripPrice(
        points_total=points_total,
        taxes_total=round(taxes_total, 2),
        voucher_eligible=eligible,
        points_per_person_voucher=points_pp_voucher,
        taxes_total_voucher=round(taxes_voucher, 2) if taxes_voucher is not None else None,
    )


# ---- cash value / cpp (Phase 4) ------------------------------------------------------------


def cpp(cash: float | None, taxes: float | None, points: int | None) -> float | None:
    """Cents of cash value per point: (cash saved − taxes still paid) × 100 / points spent."""
    if cash is None or points is None or points <= 0:
        return None
    return round((cash - (taxes or 0.0)) * 100.0 / points, 2)


@dataclass(frozen=True, slots=True)
class TripValue:
    """The cash side of a trip: what it costs to buy, and what each point is worth redeeming."""

    cash_total: float | None       # cash price per person for the same trip
    currency: str
    cash_source: str | None        # 'manual' | 'estimate' | None (no figure available)
    cpp: float | None              # cents per point, one passenger paying with points
    cpp_voucher: float | None      # with the 2-for-1: 2 pax of cash value from one award's points


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CashFareStore:
    """Manual cash quotes in the `cash_fares` table. Append-only; the latest row per
    (origin, destination, cabin, trip_type) wins, so history is a free audit trail."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def set_fare(
        self,
        origin: str,
        destination: str,
        cabin: str,
        trip_type: str,
        price: float,
        *,
        currency: str = "USD",
        source: str = "manual",
    ) -> None:
        if price <= 0:
            raise ValueError("cash price must be positive")
        if trip_type not in ("RT", "OW"):
            raise ValueError(f"trip_type must be RT or OW, got {trip_type!r}")
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO cash_fares
                   (origin, destination, cabin, trip_type, price, currency, observed_at, source)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (origin.upper(), destination.upper(), cabin, trip_type, float(price),
                 currency, _now(), source),
            )
        finally:
            conn.close()

    def latest(
        self, origin: str, destination: str, cabin: str, trip_type: str
    ) -> dict | None:
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                """SELECT * FROM cash_fares
                   WHERE origin=? AND destination=? AND cabin=? AND trip_type=?
                   ORDER BY observed_at DESC, id DESC LIMIT 1""",
                (origin.upper(), destination.upper(), cabin, trip_type),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def clear(self, origin: str, destination: str, cabin: str, trip_type: str) -> int:
        """Drop every manual quote for one key (falls back to the estimate). Returns rows removed."""
        conn = db.connect(self.db_path)
        try:
            cur = conn.execute(
                "DELETE FROM cash_fares WHERE origin=? AND destination=? AND cabin=? AND trip_type=?",
                (origin.upper(), destination.upper(), cabin, trip_type),
            )
            return cur.rowcount
        finally:
            conn.close()


class TripValueService:
    """Resolves a trip's cash price (manual quote first, zone estimate second) and its cpp."""

    def __init__(self, fares: CashFareStore, zones: ZoneTable) -> None:
        self._fares = fares
        self._zones = zones

    def cash_for(
        self,
        *,
        origin: str,
        destination: str,
        cabin: str,
        trip_type: str,
        origin_country: str | None,
        dest_country: str | None,
    ) -> tuple[float | None, str, str | None]:
        """(cash_total, currency, source) for a route+cabin+trip_type.

        Manual quotes win. A one-way with no OW quote falls back to half the round-trip figure
        (manual RT quote first, then the RT estimate — the estimates table is RT-only).
        """
        manual = self._fares.latest(origin, destination, cabin, trip_type)
        if manual:
            return float(manual["price"]), manual["currency"], "manual"
        if trip_type == "OW":
            manual_rt = self._fares.latest(origin, destination, cabin, "RT")
            if manual_rt:
                return round(float(manual_rt["price"]) / 2, 2), manual_rt["currency"], "manual"
        estimate = self._zones.rt_cash_estimate(
            origin_code=origin, origin_country=origin_country,
            dest_code=destination, dest_country=dest_country, cabin=cabin,
        )
        if estimate is None:
            return None, "USD", None
        if trip_type == "OW":
            estimate = round(estimate / 2, 2)
        return estimate, "USD", "estimate"

    def value_for(
        self,
        *,
        origin: str,
        destination: str,
        cabin: str,
        trip_type: str,
        points_total: int | None,
        taxes_total: float | None,
        voucher_eligible: bool,
        origin_country: str | None,
        dest_country: str | None,
    ) -> TripValue:
        cash, currency, source = self.cash_for(
            origin=origin, destination=destination, cabin=cabin, trip_type=trip_type,
            origin_country=origin_country, dest_country=dest_country,
        )
        plain = cpp(cash, taxes_total, points_total)
        voucher = None
        if voucher_eligible and cash is not None:
            # One award's points buy the trip for 2 pax; both still pay taxes.
            voucher = cpp(
                2 * cash, 2 * (taxes_total or 0.0), points_total,
            )
        return TripValue(
            cash_total=cash, currency=currency, cash_source=source,
            cpp=plain, cpp_voucher=voucher,
        )

    def trip_value(
        self,
        trip: TripOption,
        price: TripPrice,
        *,
        origin_country: str | None,
        dest_country: str | None,
    ) -> TripValue:
        return self.value_for(
            origin=trip.origin, destination=trip.destination, cabin=trip.cabin,
            trip_type="RT" if trip.is_round_trip else "OW",
            points_total=price.points_total, taxes_total=price.taxes_total,
            voucher_eligible=price.voucher_eligible,
            origin_country=origin_country, dest_country=dest_country,
        )
