"""Pricing + 2-for-1 voucher math tests against the seed zone table, and (Phase 4) the cash
value / cpp layer: hand-computed cpp, manual-quote precedence, and one-way halving."""
from __future__ import annotations

import pytest

from app.models import TripOption
from app.services.value import (
    CashFareStore,
    TripValueService,
    cpp,
    price_trip,
    voucher_eligible,
)


def _rt(cabin="AG", out_seats=2, in_seats=2, out_sas=True, in_sas=True) -> TripOption:
    return TripOption(
        origin="CPH", destination="BOS", cabin=cabin,
        outbound_date="2026-11-02", out_seats=out_seats,
        inbound_date="2026-11-09", in_seats=in_seats, stay_days=7,
        out_sas_operated=out_sas, in_sas_operated=in_sas,
    )


def test_round_trip_points_are_sum_of_both_legs(zones):
    trip = _rt(cabin="AB")
    price = price_trip(zones, trip, origin_country="Denmark", dest_country="United States of America")
    # Scandinavia<->North America business is 80,000 one-way in the seed table.
    assert price.points_total == 160_000
    assert price.taxes_total == 180.0  # 90 per leg


def test_voucher_halves_points_per_person(zones):
    trip = _rt(cabin="AB", out_seats=2, in_seats=2)
    price = price_trip(zones, trip, origin_country="Denmark", dest_country="United States of America")
    assert price.voucher_eligible is True
    assert price.points_per_person_voucher == 80_000   # 160k / 2
    assert price.taxes_total_voucher == 360.0          # taxes paid x2


def test_voucher_requires_two_seats_both_legs():
    assert voucher_eligible(_rt(out_seats=2, in_seats=2)) is True
    assert voucher_eligible(_rt(out_seats=2, in_seats=1)) is False
    assert voucher_eligible(_rt(out_seats=1, in_seats=2)) is False


def test_voucher_requires_both_legs_sas_operated():
    assert voucher_eligible(_rt(out_sas=True, in_sas=False)) is False
    assert voucher_eligible(_rt(out_sas=False, in_sas=True)) is False


def test_one_way_is_never_voucher_eligible():
    ow = TripOption(origin="CPH", destination="BOS", cabin="AB",
                    outbound_date="2026-11-02", out_seats=4)
    assert voucher_eligible(ow) is False


def test_one_way_price_is_single_leg(zones):
    ow = TripOption(origin="CPH", destination="BOS", cabin="AB",
                    outbound_date="2026-11-02", out_seats=4)
    price = price_trip(zones, ow, origin_country="Denmark", dest_country="United States of America")
    assert price.points_total == 80_000
    assert price.taxes_total == 90.0
    assert price.voucher_eligible is False


def test_uk_departure_surcharge_makes_taxes_asymmetric(zones):
    """A round trip touching the UK pays APD on the UK-departing (inbound) leg only."""
    trip = TripOption(
        origin="CPH", destination="LHR", cabin="AB",
        outbound_date="2026-11-02", out_seats=2, inbound_date="2026-11-09", in_seats=2, stay_days=7,
    )
    price = price_trip(zones, trip, origin_country="Denmark", dest_country="United Kingdom")
    # Base Scandinavia<->Europe tax 20/leg = 40; UK APD business surcharge 220 on the return leg.
    assert price.taxes_total == 20 + (20 + 220)
    # Europe business points 32,000 one-way -> 64,000 r/t.
    assert price.points_total == 64_000


def test_zone_resolution_prefers_airport_override(zones):
    # CPH is pinned to SCANDINAVIA via airport_zones even with no country given.
    assert zones.zone_for("CPH", None) == "SCANDINAVIA"
    # Unknown country falls back to default zone.
    assert zones.zone_for("ZZZ", "Nowhereland") == zones._default_zone


# ---- cash value / cpp (Phase 4) ------------------------------------------------------------

US = "United States of America"


def test_cpp_hand_computed():
    # $2,800 cash, $180 taxes still paid, 160,000 points: (2800-180)*100/160000 = 1.6375.
    assert cpp(2800, 180, 160_000) == 1.64
    # Voucher framing: 2 pax of cash value, 2x taxes, same points: (5600-360)*100/160000 = 3.275
    # (float representation puts 3.275 a hair below the half, so round() lands on 3.27).
    assert cpp(2 * 2800, 2 * 180, 160_000) == 3.27


def test_cpp_guards():
    assert cpp(None, 90, 80_000) is None
    assert cpp(700, 90, None) is None
    assert cpp(700, 90, 0) is None
    assert cpp(700, None, 70_000) == 1.0    # missing taxes treated as 0


def test_rt_cash_estimate_from_zone_table(zones):
    # Scandinavia<->North America business seed estimate is $2,800 round-trip.
    est = zones.rt_cash_estimate(
        origin_code="CPH", origin_country="Denmark",
        dest_code="BOS", dest_country=US, cabin="AB",
    )
    assert est == 2800.0


@pytest.fixture
def values(tmp_db, zones) -> TripValueService:
    return TripValueService(CashFareStore(tmp_db), zones)


def test_trip_value_uses_estimate_until_manual_quote(values, zones, tmp_db):
    trip = _rt(cabin="AB")
    price = price_trip(zones, trip, origin_country="Denmark", dest_country=US)
    v = values.trip_value(trip, price, origin_country="Denmark", dest_country=US)
    assert (v.cash_total, v.cash_source) == (2800.0, "estimate")
    assert v.cpp == 1.64                      # (2800-180)*100/160000
    assert v.cpp_voucher == 3.27              # voucher-eligible RT: 2 pax value, 2x taxes

    # A manual quote overrides the estimate — and clears back to it.
    fares = CashFareStore(tmp_db)
    fares.set_fare("CPH", "BOS", "AB", "RT", 3400)
    v = values.trip_value(trip, price, origin_country="Denmark", dest_country=US)
    assert (v.cash_total, v.cash_source) == (3400.0, "manual")
    assert v.cpp == round((3400 - 180) * 100 / 160_000, 2)
    fares.clear("CPH", "BOS", "AB", "RT")
    v = values.trip_value(trip, price, origin_country="Denmark", dest_country=US)
    assert v.cash_source == "estimate"


def test_latest_manual_quote_wins(values, tmp_db):
    fares = CashFareStore(tmp_db)
    fares.set_fare("CPH", "BOS", "AB", "RT", 3000)
    fares.set_fare("CPH", "BOS", "AB", "RT", 2600)
    assert fares.latest("CPH", "BOS", "AB", "RT")["price"] == 2600


def test_one_way_value_halves_rt_figures(values, zones, tmp_db):
    ow = TripOption(origin="CPH", destination="BOS", cabin="AB",
                    outbound_date="2026-11-02", out_seats=4)
    price = price_trip(zones, ow, origin_country="Denmark", dest_country=US)
    v = values.trip_value(ow, price, origin_country="Denmark", dest_country=US)
    # No OW figures anywhere -> half the RT estimate; never voucher cpp on a one-way.
    assert (v.cash_total, v.cash_source) == (1400.0, "estimate")
    assert v.cpp_voucher is None

    # A manual RT quote halves too; a manual OW quote beats both.
    fares = CashFareStore(tmp_db)
    fares.set_fare("CPH", "BOS", "AB", "RT", 3000)
    v = values.trip_value(ow, price, origin_country="Denmark", dest_country=US)
    assert (v.cash_total, v.cash_source) == (1500.0, "manual")
    fares.set_fare("CPH", "BOS", "AB", "OW", 1800)
    v = values.trip_value(ow, price, origin_country="Denmark", dest_country=US)
    assert (v.cash_total, v.cash_source) == (1800.0, "manual")


def test_cash_fare_store_validates():
    store = CashFareStore(None)  # validation fires before any DB access
    with pytest.raises(ValueError):
        store.set_fare("CPH", "BOS", "AB", "RT", 0)
    with pytest.raises(ValueError):
        store.set_fare("CPH", "BOS", "AB", "XX", 100)
