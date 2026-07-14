"""Scenario table for the per-leg diffing engine (plan §Phase 2 verification)."""
from __future__ import annotations

from app.models import AwardFlight
from app.services.diffing import CurrentLeg, diff_legs
from tests.conftest import make_flight


def leg(f: AwardFlight) -> CurrentLeg:
    """The award_current row a previous sweep would have left behind for this flight."""
    return CurrentLeg(
        flight_key=f.key, origin=f.origin, destination=f.destination, direction=f.direction,
        flight_date=f.flight_date, cabin=f.cabin, seats=f.seats,
        is_sas_operated=f.is_sas_operated,
    )


BASE = make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=1)


def test_new_key_is_opened():
    diff = diff_legs([], [BASE], sweep_ok=True)
    assert [f.key for f in diff.opened] == [BASE.key]
    assert not diff.closed
    assert diff.baseline  # nothing previously known for the scope


def test_unchanged_key_is_quiet():
    diff = diff_legs([leg(BASE)], [BASE], sweep_ok=True)
    assert not diff.has_changes
    assert not diff.baseline


def test_seat_count_change_alone_is_quiet():
    now = make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=1)
    prev = make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=4)
    diff = diff_legs([leg(prev)], [now], sweep_ok=True)
    assert not diff.has_changes  # 4 -> 1 seats: same key, no opened/closed/voucher event


def test_missing_key_is_closed_when_sweep_ok():
    diff = diff_legs([leg(BASE)], [], sweep_ok=True)
    assert [c.flight_key for c in diff.closed] == [BASE.key]


def test_partial_sweep_never_emits_closed():
    diff = diff_legs([leg(BASE)], [], sweep_ok=False)
    assert diff.closed == []


def test_voucher_pair_on_seat_transition():
    prev = make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=1)
    now = make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=2)
    diff = diff_legs([leg(prev)], [now], sweep_ok=True)
    assert [f.key for f in diff.voucher_pairs] == [now.key]
    assert not diff.opened  # same key: a transition, not an opening


def test_voucher_pair_requires_sas_operated():
    prev = make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=1, sas=False)
    now = make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=3, sas=False)
    diff = diff_legs([leg(prev)], [now], sweep_ok=True)
    assert diff.voucher_pairs == []


def test_opening_straight_into_two_seats_is_also_a_voucher_pair():
    now = make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=2)
    diff = diff_legs([], [now], sweep_ok=True)
    assert [f.key for f in diff.opened] == [now.key]
    assert [f.key for f in diff.voucher_pairs] == [now.key]


def test_mixed_scenario():
    """One stays, one closes, one opens, one crosses the voucher threshold."""
    stays = make_flight(direction="outbound", date="2026-11-02", cabin="AG", seats=5)
    closes = make_flight(direction="inbound", date="2026-11-09", cabin="AB", seats=1)
    was_one = make_flight(direction="outbound", date="2026-11-03", cabin="AB", seats=1)
    now_two = make_flight(direction="outbound", date="2026-11-03", cabin="AB", seats=2)
    opens = make_flight(direction="inbound", date="2026-11-12", cabin="AG", seats=3)

    diff = diff_legs(
        [leg(stays), leg(closes), leg(was_one)], [stays, now_two, opens], sweep_ok=True
    )
    assert [f.key for f in diff.opened] == [opens.key]
    assert [c.flight_key for c in diff.closed] == [closes.key]
    # now_two crossed the threshold; opens arrived straight into >=2 seats — both are candidates.
    assert {f.key for f in diff.voucher_pairs} == {now_two.key, opens.key}
