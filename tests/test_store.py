"""Snapshot store: persist -> read-back, catalog upsert, and award_current upsert idempotency."""
from __future__ import annotations

import pytest

from app import db
from app.providers.base import SCOPE_ROUTE, ProviderFetch
from app.providers.sas_direct.parser import parse_feed
from app.services.snapshots import SnapshotStore


@pytest.fixture
def store(tmp_path):
    dbp = tmp_path / "af.db"
    snaps = tmp_path / "snapshots"
    db.init_db(dbp)
    s = SnapshotStore(dbp, snaps)
    s.seed_home_airports(["CPH"])
    return s


def _pf(route_bos_raw: str) -> ProviderFetch:
    feed = parse_feed(route_bos_raw, "CPH")
    return ProviderFetch(
        scope=SCOPE_ROUTE, origin="CPH", destination="BOS", feed=feed,
        raw_text=route_bos_raw, http_status=200, byte_size=len(route_bos_raw),
        duration_ms=120, status="ok",
    )


def test_persist_and_read_back(store, route_bos_raw):
    pf = _pf(route_bos_raw)
    sid = store.persist(pf)
    assert sid > 0
    snap = store.latest_snapshot("CPH", SCOPE_ROUTE, "BOS")
    assert snap is not None
    assert snap["status"] == "ok"
    assert snap["dest_count"] == 1
    flights = store.flights_by_snapshot(snap["id"])
    assert len(flights) == len(pf.feed.flights)


def test_freshness(store, route_bos_raw):
    store.persist(_pf(route_bos_raw))
    snap = store.latest_snapshot("CPH", SCOPE_ROUTE, "BOS")
    assert store.is_fresh(snap, ttl_s=900) is True
    assert store.is_fresh(snap, ttl_s=0) is False
    assert store.is_fresh(None, ttl_s=900) is False


def test_catalog_and_country(store, route_bos_raw):
    store.persist(_pf(route_bos_raw))
    assert store.country_for("BOS") == "United States of America"
    assert store.home_airports() == ["CPH"]
    dests = {d["code"] for d in store.list_destinations()}
    assert "BOS" in dests
    assert "CPH" not in dests  # home airports excluded from the destination picker


def test_award_current_upsert_is_idempotent(store, route_bos_raw):
    pf = _pf(route_bos_raw)
    store.persist(pf)
    store.persist(pf)
    conn = db.connect(store.db_path)
    try:
        n_current = conn.execute("SELECT COUNT(*) FROM award_current").fetchone()[0]
        n_flights = conn.execute("SELECT COUNT(*) FROM award_flights").fetchone()[0]
    finally:
        conn.close()
    assert n_current == len(pf.feed.flights)          # unique per flight_key
    assert n_flights == len(pf.feed.flights) * 2      # append-only history


def test_home_airport_not_clobbered_by_catalog_upsert(store, route_bos_raw):
    """Persisting a destination that is also a home airport must not flip is_home to 0."""
    store.seed_home_airports(["BOS"])  # pretend BOS is a home
    store.persist(_pf(route_bos_raw))  # feed also contains BOS as a destination
    assert "BOS" in store.home_airports()
