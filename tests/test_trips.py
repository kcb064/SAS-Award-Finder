"""Round-trip pairing tests: stay-window bounds, both-legs-required, cheapest/best pick,
no-return-in-window => no trip. Uses hand-built flights for determinism."""
from __future__ import annotations

from tests.conftest import make_flight

from app.services import trips as T


def test_basic_pairing_within_stay_window():
    flights = [
        make_flight(direction="outbound", date="2026-11-02", cabin="AG", seats=4),
        make_flight(direction="inbound", date="2026-11-09", cabin="AG", seats=3),   # stay 7 -> ok
        make_flight(direction="inbound", date="2026-11-03", cabin="AG", seats=3),   # stay 1 -> too short
        make_flight(direction="inbound", date="2026-12-20", cabin="AG", seats=3),   # stay 48 -> too long
    ]
    opts = T.pair_round_trips(flights, cabin="AG", min_stay_days=3, max_stay_days=21)
    assert len(opts) == 1
    o = opts[0]
    assert o.outbound_date == "2026-11-02"
    assert o.inbound_date == "2026-11-09"
    assert o.stay_days == 7
    assert o.out_seats == 4 and o.in_seats == 3


def test_both_legs_must_meet_min_seats():
    flights = [
        make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=2),
        make_flight(direction="inbound", date="2026-11-09", cabin="AB", seats=1),  # only 1 seat back
    ]
    # min_seats=2 -> the return fails, so no round-trip
    assert T.pair_round_trips(flights, cabin="AB", min_seats=2, min_stay_days=1, max_stay_days=30) == []
    # min_seats=1 -> both legs qualify
    assert len(T.pair_round_trips(flights, cabin="AB", min_seats=1, min_stay_days=1, max_stay_days=30)) == 1


def test_no_return_in_window_means_no_trip():
    flights = [
        make_flight(direction="outbound", date="2026-11-02", cabin="AG", seats=4),
        make_flight(direction="inbound", date="2026-11-09", cabin="AG", seats=4),
    ]
    # Return window excludes the only inbound date -> no trip.
    opts = T.pair_round_trips(
        flights, cabin="AG", ret_from="2026-12-01", ret_to="2026-12-31",
        min_stay_days=1, max_stay_days=90,
    )
    assert opts == []


def test_outbound_window_filters():
    flights = [
        make_flight(direction="outbound", date="2026-11-02", cabin="AG", seats=4),
        make_flight(direction="outbound", date="2026-11-20", cabin="AG", seats=4),
        make_flight(direction="inbound", date="2026-11-25", cabin="AG", seats=4),
    ]
    opts = T.pair_round_trips(
        flights, cabin="AG", out_from="2026-11-15", out_to="2026-11-30",
        min_stay_days=1, max_stay_days=30,
    )
    assert [o.outbound_date for o in opts] == ["2026-11-20"]


def test_cabins_do_not_cross():
    flights = [
        make_flight(direction="outbound", date="2026-11-02", cabin="AG", seats=4),
        make_flight(direction="inbound", date="2026-11-09", cabin="AB", seats=4),  # different cabin
    ]
    # No AG inbound and no AB outbound -> nothing pairs.
    assert T.pair_round_trips(flights, min_stay_days=1, max_stay_days=30) == []


def test_stay_bounds_inclusive():
    flights = [
        make_flight(direction="outbound", date="2026-11-01", cabin="AG", seats=2),
        make_flight(direction="inbound", date="2026-11-06", cabin="AG", seats=2),  # exactly 5
        make_flight(direction="inbound", date="2026-11-11", cabin="AG", seats=2),  # exactly 10
    ]
    opts = T.pair_round_trips(flights, cabin="AG", min_stay_days=5, max_stay_days=10)
    assert {o.stay_days for o in opts} == {5, 10}


def test_best_return_per_outbound_prefers_more_seats_then_shorter_stay():
    flights = [
        make_flight(direction="outbound", date="2026-11-02", cabin="AG", seats=9),
        make_flight(direction="inbound", date="2026-11-09", cabin="AG", seats=2),  # stay 7, 2 seats
        make_flight(direction="inbound", date="2026-11-12", cabin="AG", seats=6),  # stay 10, 6 seats
        make_flight(direction="inbound", date="2026-11-16", cabin="AG", seats=6),  # stay 14, 6 seats
    ]
    all_opts = T.pair_round_trips(flights, cabin="AG", min_stay_days=1, max_stay_days=30)
    best = T.best_round_trip_per_outbound(all_opts)
    assert len(best) == 1
    # min(out,in) seats: 6 beats 2; among the two 6-seat returns, shorter stay (10) wins.
    assert best[0].inbound_date == "2026-11-12"
    assert best[0].stay_days == 10


def test_one_way_options_filter_by_seats_and_window():
    flights = [
        make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=2),
        make_flight(direction="outbound", date="2026-11-05", cabin="AB", seats=1),
        make_flight(direction="inbound", date="2026-11-09", cabin="AB", seats=5),  # ignored for OW
    ]
    ow = T.one_way_options(flights, cabin="AB", min_seats=2)
    assert [o.outbound_date for o in ow] == ["2026-11-02"]
    assert all(not o.is_round_trip for o in ow)


def test_pairing_over_real_fixture(route_bos_json):
    from app.providers.sas_direct.parser import parse_feed

    feed = parse_feed(route_bos_json, "CPH")
    opts = T.pair_round_trips(
        feed.flights, cabin="AG", out_from="2026-11-01", out_to="2026-11-10",
        ret_from="2026-11-05", ret_to="2026-11-30", min_stay_days=5, max_stay_days=21,
    )
    assert opts, "economy has plenty of both-direction availability in the fixture"
    for o in opts:
        assert o.is_round_trip
        assert 5 <= o.stay_days <= 21
        assert o.cabin == "AG"
