"""Parser contract tests — every AwardFlight field, and both directions from one response."""
from __future__ import annotations

import pytest

from app.providers.sas_direct.parser import FeedParseError, parse_feed


def test_route_feed_parses_both_directions(route_bos_json):
    feed = parse_feed(route_bos_json, "CPH")
    directions = {f.direction for f in feed.flights}
    assert directions == {"outbound", "inbound"}, "route feed must yield both legs"
    assert any(f.direction == "outbound" for f in feed.flights)
    assert any(f.direction == "inbound" for f in feed.flights)


def test_network_feed_is_outbound_only(network_json):
    feed = parse_feed(network_json, "CPH")
    assert feed.flights, "network feed should still have outbound flights"
    assert all(f.direction == "outbound" for f in feed.flights), (
        "network feed carries no inbound availability (Phase 1 finding)"
    )
    assert {d.code for d in feed.destinations} == {"BOS", "EWR", "LHR", "CDG", "OSL"}


def test_every_award_flight_field(route_bos_json):
    feed = parse_feed(route_bos_json, "CPH")
    # A known outbound entry from the fixture: 2026-11-02 total 20, AG10 AP8 AB2.
    nov2 = {
        f.cabin: f
        for f in feed.flights
        if f.direction == "outbound" and f.flight_date == "2026-11-02"
    }
    assert set(nov2) == {"AG", "AP", "AB"}
    ab = nov2["AB"]
    assert ab.origin == "CPH"
    assert ab.destination == "BOS"
    assert ab.direction == "outbound"
    assert ab.flight_date == "2026-11-02"
    assert ab.cabin == "AB"
    assert ab.seats == 2
    assert ab.seats_total == 20
    assert ab.is_sas_operated is True
    assert len(ab.key) == 40  # sha1 hex


def test_cabin_key_absent_means_no_row(route_bos_json):
    """Dates with no seats in a cabin (cabin key absent) must not produce a row for that cabin."""
    feed = parse_feed(route_bos_json, "CPH")
    # 2026-10-05 outbound is AG-only (AG10) in the fixture — no AP/AB rows.
    oct5 = [
        f for f in feed.flights
        if f.direction == "outbound" and f.flight_date == "2026-10-05"
    ]
    assert {f.cabin for f in oct5} == {"AG"}


def test_flight_key_is_stable_and_direction_sensitive(route_bos_json):
    from app.models import flight_key

    k_out = flight_key("outbound", "2026-11-02", "CPH", "BOS", "AB")
    k_in = flight_key("inbound", "2026-11-02", "CPH", "BOS", "AB")
    assert k_out != k_in
    assert flight_key("outbound", "2026-11-02", "CPH", "BOS", "AB") == k_out  # deterministic


def test_origin_is_applied_and_uppercased(route_bos_json):
    feed = parse_feed(route_bos_json, "cph")
    assert all(f.origin == "CPH" for f in feed.flights)


def test_bad_payload_raises():
    with pytest.raises(FeedParseError):
        parse_feed({"not": "a list"}, "CPH")
    with pytest.raises(FeedParseError):
        parse_feed([{"noAirportCode": True}], "CPH")


def test_accepts_raw_json_string(route_bos_raw):
    feed = parse_feed(route_bos_raw, "CPH")
    assert feed.flights
