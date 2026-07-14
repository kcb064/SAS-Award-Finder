"""Explore: lead computation, overview ranking, and the budgeted sweeper's determinism.

The sweeper tests are the Phase 3 verification gate: a sweep with budget N performs exactly N
provider fetches, rotation picks never-fetched routes first then the stalest, and a fresh cached
snapshot never burns budget.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import make_flight

from app import db
from app.fetch.budget import BudgetExceeded
from app.models import DestinationInfo, ParsedFeed
from app.providers.base import SCOPE_NETWORK, ProviderFetch
from app.services.explore import (
    CabinAvailability,
    ExploreStore,
    ExploreSweeper,
    availability_score,
    compute_leads,
)
from app.services.notify import AlertStore
from app.services.snapshots import SnapshotStore

# ---- pure: compute_leads ------------------------------------------------------------------


def test_compute_leads_picks_most_bookable_per_month(zones):
    flights = [
        make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=2),
        make_flight(direction="outbound", date="2026-11-10", cabin="AB", seats=4),
        make_flight(direction="inbound", date="2026-11-09", cabin="AB", seats=2),
        make_flight(direction="inbound", date="2026-11-20", cabin="AB", seats=1),
    ]
    leads = compute_leads(flights, zones, origin_country=None, dest_country=None)
    assert len(leads) == 1  # one (AB, 2026-11) bucket
    lead = leads[0]
    assert (lead.cabin, lead.month) == ("AB", "2026-11")
    # The voucher-eligible pair (2 seats both legs) beats the 4/1-seat pair.
    assert (lead.outbound_date, lead.inbound_date) == ("2026-11-02", "2026-11-09")
    assert lead.voucher_eligible is True
    assert lead.stay_days == 7
    assert lead.points_total and lead.points_total > 0


def test_compute_leads_requires_a_return(zones):
    flights = [
        # November pairs fine; December has outbound space but no return at all.
        make_flight(direction="outbound", date="2026-11-02", cabin="AG", seats=1),
        make_flight(direction="inbound", date="2026-11-09", cabin="AG", seats=1),
        make_flight(direction="outbound", date="2026-12-05", cabin="AG", seats=3),
    ]
    leads = compute_leads(flights, zones, origin_country=None, dest_country=None)
    assert [l.month for l in leads] == ["2026-11"]


def test_compute_leads_respects_stay_window(zones):
    flights = [
        make_flight(direction="outbound", date="2026-11-02", cabin="AG", seats=2),
        make_flight(direction="inbound", date="2026-11-04", cabin="AG", seats=2),   # 2-day stay
        make_flight(direction="inbound", date="2026-11-30", cabin="AG", seats=2),   # 28-day stay
    ]
    assert compute_leads(flights, zones, origin_country=None, dest_country=None) == []
    widened = compute_leads(
        flights, zones, origin_country=None, dest_country=None,
        min_stay_days=2, max_stay_days=30,
    )
    # Both stays now qualify; the bucket keeps one best (same seats -> earliest return wins
    # via earliest outbound tie, then _bookability's date ordering).
    assert len(widened) == 1


def test_compute_leads_buckets_by_cabin_and_month(zones):
    flights = [
        make_flight(direction="outbound", date="2026-11-02", cabin="AG", seats=1),
        make_flight(direction="inbound", date="2026-11-09", cabin="AG", seats=1),
        make_flight(direction="outbound", date="2026-11-03", cabin="AB", seats=2),
        make_flight(direction="inbound", date="2026-11-10", cabin="AB", seats=2),
        make_flight(direction="outbound", date="2026-12-05", cabin="AG", seats=1),
        make_flight(direction="inbound", date="2026-12-12", cabin="AG", seats=1),
    ]
    leads = compute_leads(flights, zones, origin_country=None, dest_country=None)
    assert [(l.month, l.cabin) for l in leads] == [
        ("2026-11", "AB"), ("2026-11", "AG"), ("2026-12", "AG"),
    ]


# ---- pure: scoring --------------------------------------------------------------------------


def test_availability_score_weights_business_and_caps_days():
    a_year_of_economy = {"AG": CabinAvailability(days=300, pair_days=0, max_seats=9)}
    a_month_of_business = {"AB": CabinAvailability(days=30, pair_days=30, max_seats=2)}
    assert availability_score(a_year_of_economy) == 90            # capped at DAYS_CAP
    assert availability_score(a_month_of_business) == 180         # 4*30 + 4*0.5*30
    assert availability_score(a_month_of_business) > availability_score(a_year_of_economy)


# ---- store: overview / interest / queue ----------------------------------------------------


DESTS = {
    "BOS": DestinationInfo(code="BOS", city_name="Boston", country_name="United States of America"),
    "FCO": DestinationInfo(code="FCO", city_name="Rome", country_name="Italy"),
    "NRT": DestinationInfo(code="NRT", city_name="Tokyo", country_name="Japan"),
}


def network_flights(dest: str, *, cabin: str = "AB", days: int = 5, seats: int = 2):
    return [
        make_flight(
            direction="outbound", date=f"2026-11-{d + 1:02d}", cabin=cabin, seats=seats,
            destination=dest,
        )
        for d in range(days)
    ]


def route_feed(dest: str, *, cabin: str = "AB", seats: int = 2):
    return [
        make_flight(direction="outbound", date="2026-11-02", cabin=cabin, seats=seats,
                    destination=dest),
        make_flight(direction="inbound", date="2026-11-09", cabin=cabin, seats=seats,
                    destination=dest),
    ]


def persist_network(snapshots: SnapshotStore, flights, dests, origin: str = "CPH") -> int:
    pf = ProviderFetch(
        scope=SCOPE_NETWORK, origin=origin, destination=None,
        feed=ParsedFeed(destinations=list(dests), flights=list(flights)),
        raw_text="[]", http_status=200, byte_size=2, duration_ms=1,
    )
    return snapshots.persist(pf)


@pytest.fixture
def explore_env(tmp_db: Path, tmp_path: Path, zones):
    snapshots = SnapshotStore(tmp_db, tmp_path / "snaps")
    snapshots.seed_home_airports(["CPH"])
    store = ExploreStore(tmp_db, snapshots, zones)
    return snapshots, store


def test_overview_empty_before_first_network_snapshot(explore_env):
    _, store = explore_env
    rows, snap = store.overview("CPH")
    assert rows == [] and snap is None


def test_overview_ranks_by_interest_times_availability(explore_env):
    snapshots, store = explore_env
    flights = network_flights("BOS", cabin="AB", days=10) + \
        network_flights("FCO", cabin="AG", days=5, seats=1)
    persist_network(snapshots, flights, [DESTS["BOS"], DESTS["FCO"]])

    rows, snap = store.overview("CPH")
    assert snap is not None
    assert [r.code for r in rows] == ["BOS", "FCO"]           # business availability dominates
    bos = rows[0]
    assert bos.cabins["AB"].days == 10 and bos.cabins["AB"].pair_days == 10
    assert bos.est_points["AB"] > 0
    assert bos.city_name == "Boston"

    # Zeroing interest drops the score to 0 and sinks it, but the row stays visible.
    store.set_interest("BOS", 0)
    rows, _ = store.overview("CPH")
    assert [r.code for r in rows] == ["FCO", "BOS"]
    assert rows[1].score == 0


def test_overview_resolves_region_from_zone_table(explore_env):
    snapshots, store = explore_env
    flights = (
        network_flights("BOS", cabin="AB", days=3)
        + network_flights("FCO", cabin="AG", days=3)
        + network_flights("NRT", cabin="AG", days=3)
    )
    persist_network(snapshots, flights, DESTS.values())
    rows, _ = store.overview("CPH")
    # Region comes from the points-table zones (country -> zone), the Explore page's filter axis.
    assert {r.code: r.region for r in rows} == {
        "BOS": "NORTH_AMERICA", "FCO": "EUROPE", "NRT": "ASIA",
    }


def test_set_interest_rejects_out_of_range(explore_env):
    _, store = explore_env
    with pytest.raises(ValueError):
        store.set_interest("BOS", 4)
    with pytest.raises(ValueError):
        store.set_interest("BOS", -1)


def test_sweep_queue_prefers_never_fetched_then_stalest(explore_env):
    snapshots, store = explore_env
    flights = (
        network_flights("BOS", cabin="AB", days=10)
        + network_flights("FCO", cabin="AB", days=5)
        + network_flights("NRT", cabin="AG", days=5, seats=1)
    )
    persist_network(snapshots, flights, DESTS.values())
    assert store.sweep_queue("CPH") == ["BOS", "FCO", "NRT"]  # score order, none fetched yet

    # A route snapshot (e.g. Kevin searched BOS) sends BOS to the back of the queue.
    pf = ProviderFetch(
        scope="route", origin="CPH", destination="BOS",
        feed=ParsedFeed(destinations=[], flights=route_feed("BOS")),
        raw_text="[]", http_status=200, byte_size=2, duration_ms=1,
    )
    snapshots.persist(pf)
    assert store.sweep_queue("CPH") == ["FCO", "NRT", "BOS"]

    # Interest 0 removes a destination from the queue entirely.
    store.set_interest("FCO", 0)
    assert store.sweep_queue("CPH") == ["NRT", "BOS"]


# ---- sweeper --------------------------------------------------------------------------------


class RouteProvider:
    """Serves canned route feeds; can be armed to raise BudgetExceeded on the Nth fetch."""

    name = "fake"
    capabilities = {"network", "route"}

    def __init__(self, feeds: dict[str, list]) -> None:
        self.feeds = feeds
        self.calls: list[str] = []
        self.raise_budget_on_call: int | None = None

    async def fetch(self, scope, origin, destination=None):
        if self.raise_budget_on_call is not None and len(self.calls) + 1 >= self.raise_budget_on_call:
            raise BudgetExceeded("daily budget spent (test)")
        self.calls.append(destination)
        return ProviderFetch(
            scope=scope, origin=origin, destination=destination,
            feed=ParsedFeed(destinations=[], flights=list(self.feeds[destination])),
            raw_text="[]", http_status=200, byte_size=2, duration_ms=1,
        )


def build_sweeper(tmp_db, tmp_path, zones, *, ttl_s: int, budget: int):
    snapshots = SnapshotStore(tmp_db, tmp_path / "snaps")
    snapshots.seed_home_airports(["CPH"])
    store = ExploreStore(tmp_db, snapshots, zones)
    alerts = AlertStore(tmp_db)
    flights = (
        network_flights("BOS", cabin="AB", days=10)
        + network_flights("FCO", cabin="AB", days=5)
        + network_flights("NRT", cabin="AB", days=3)
    )
    persist_network(snapshots, flights, DESTS.values())
    provider = RouteProvider({d: route_feed(d) for d in DESTS})
    sweeper = ExploreSweeper(
        provider, snapshots, store, zones, alerts,
        snapshot_ttl_s=ttl_s, per_run_budget=budget,
    )
    return sweeper, provider, store, alerts


def sweep_run_rows(tmp_db):
    conn = db.connect(tmp_db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM sweep_runs WHERE kind='explore' ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


async def test_sweeper_budget_caps_fetches_and_rotates(tmp_db, tmp_path, zones):
    # ttl 0: cached snapshots are never fresh, so every refresh is a real fetch — the budget
    # math and the rotation order become exact.
    sweeper, provider, store, _ = build_sweeper(tmp_db, tmp_path, zones, ttl_s=0, budget=2)

    summary = await sweeper.run_origin("CPH")
    assert provider.calls == ["BOS", "FCO"]                    # queue order, stopped at budget
    assert summary == {"queued": 3, "fetched": 2, "cached": 0, "leads": 2, "failed": 0}
    assert set(store.leads_for("CPH")) == {"BOS", "FCO"}

    summary = await sweeper.run_origin("CPH")
    # NRT was never fetched -> first; then the stalest of the fetched (BOS).
    assert provider.calls == ["BOS", "FCO", "NRT", "BOS"]
    assert summary["fetched"] == 2
    assert set(store.leads_for("CPH")) == {"BOS", "FCO", "NRT"}

    runs = sweep_run_rows(tmp_db)
    assert len(runs) == 2
    assert all(r["status"] == "ok" for r in runs)
    assert "fetched=2" in runs[0]["notes"]


async def test_sweeper_fresh_snapshots_do_not_burn_budget(tmp_db, tmp_path, zones):
    sweeper, provider, store, _ = build_sweeper(tmp_db, tmp_path, zones, ttl_s=900, budget=2)

    await sweeper.run_origin("CPH")
    assert provider.calls == ["BOS", "FCO"]

    # Second run: NRT is fetched (1 request); BOS and FCO snapshots are still fresh, so their
    # leads recompute from cache — refreshed without spending budget.
    summary = await sweeper.run_origin("CPH")
    assert provider.calls == ["BOS", "FCO", "NRT"]
    assert summary == {"queued": 3, "fetched": 1, "cached": 2, "leads": 3, "failed": 0}
    assert set(store.leads_for("CPH")) == {"BOS", "FCO", "NRT"}


async def test_sweeper_stops_on_daily_budget_and_alerts_ops(tmp_db, tmp_path, zones):
    sweeper, provider, store, alerts = build_sweeper(tmp_db, tmp_path, zones, ttl_s=0, budget=3)
    provider.raise_budget_on_call = 2                          # first fetch ok, second raises

    summary = await sweeper.run_origin("CPH")                  # must not raise
    assert summary["fetched"] == 1 and summary["failed"] == 0
    assert set(store.leads_for("CPH")) == {"BOS"}
    ops = [a for a in alerts.recent() if a["type"] == "ops"]
    assert len(ops) == 1 and "budget" in ops[0]["title"] + ops[0]["body"]
    assert sweep_run_rows(tmp_db)[-1]["status"] == "partial"


async def test_refresh_destination_replaces_stale_leads(tmp_db, tmp_path, zones):
    sweeper, provider, store, _ = build_sweeper(tmp_db, tmp_path, zones, ttl_s=0, budget=5)

    count, cached = await sweeper.refresh_destination("CPH", "BOS")
    assert (count, cached) == (1, False)
    assert store.leads_for("CPH")["BOS"][0]["month"] == "2026-11"

    # The route's space moves to December: the November lead must disappear, not linger.
    provider.feeds["BOS"] = [
        make_flight(direction="outbound", date="2026-12-05", cabin="AB", seats=2, destination="BOS"),
        make_flight(direction="inbound", date="2026-12-12", cabin="AB", seats=2, destination="BOS"),
    ]
    count, _ = await sweeper.refresh_destination("CPH", "BOS")
    assert count == 1
    months = [l["month"] for l in store.leads_for("CPH")["BOS"]]
    assert months == ["2026-12"]
