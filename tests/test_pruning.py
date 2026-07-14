"""Snapshot pruning: retention window, latest-per-key survival, raw-file cleanup, and
departed-date hygiene for award_current / explore_leads."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db
from app.providers.base import SCOPE_ROUTE, ProviderFetch
from app.providers.sas_direct.parser import parse_feed
from app.services.snapshots import SnapshotStore


@pytest.fixture
def store(tmp_path):
    dbp = tmp_path / "af.db"
    db.init_db(dbp)
    s = SnapshotStore(dbp, tmp_path / "snapshots")
    s.seed_home_airports(["CPH"])
    return s


def _pf(route_bos_raw: str) -> ProviderFetch:
    feed = parse_feed(route_bos_raw, "CPH")
    return ProviderFetch(
        scope=SCOPE_ROUTE, origin="CPH", destination="BOS", feed=feed,
        raw_text=route_bos_raw, http_status=200, byte_size=len(route_bos_raw),
        duration_ms=120, status="ok",
    )


def _age_snapshot(store: SnapshotStore, snapshot_id: int, days: int) -> None:
    """Backdate a snapshot's fetched_at so it falls outside the retention window."""
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = db.connect(store.db_path)
    try:
        conn.execute(
            "UPDATE availability_snapshots SET fetched_at = ? WHERE id = ?", (old, snapshot_id)
        )
    finally:
        conn.close()


def _snapshot_ids(store: SnapshotStore) -> set[int]:
    conn = db.connect(store.db_path)
    try:
        return {r["id"] for r in conn.execute("SELECT id FROM availability_snapshots")}
    finally:
        conn.close()


def test_prune_keeps_window_and_latest_per_route(store, route_bos_raw):
    s1 = store.persist(_pf(route_bos_raw))     # ancient
    s2 = store.persist(_pf(route_bos_raw))     # old but the route's latest until s3
    s3 = store.persist(_pf(route_bos_raw))     # fresh
    _age_snapshot(store, s1, 90)
    _age_snapshot(store, s2, 40)

    summary = store.prune(30)
    assert summary["snapshots"] == 2
    assert summary["flights"] > 0
    assert _snapshot_ids(store) == {s3}
    # The kept snapshot still reads back for Search.
    snap = store.latest_snapshot("CPH", SCOPE_ROUTE, "BOS")
    assert snap["id"] == s3
    assert store.flights_by_snapshot(s3)


def test_prune_never_drops_a_routes_only_snapshot(store, route_bos_raw):
    """Even a months-old snapshot survives while it's the newest for its (origin, scope, dest) —
    the app was simply down; Search/Explore must keep something to read."""
    s1 = store.persist(_pf(route_bos_raw))
    _age_snapshot(store, s1, 200)
    summary = store.prune(30)
    assert summary["snapshots"] == 0
    assert _snapshot_ids(store) == {s1}


def test_prune_removes_raw_files_of_dropped_snapshots(store, route_bos_raw):
    s1 = store.persist(_pf(route_bos_raw))
    import time as _time
    _time.sleep(1.1)                            # raw filenames are second-granular timestamps
    store.persist(_pf(route_bos_raw))
    _age_snapshot(store, s1, 90)

    conn = db.connect(store.db_path)
    try:
        paths = {
            r["id"]: Path(r["raw_path"])
            for r in conn.execute("SELECT id, raw_path FROM availability_snapshots")
        }
    finally:
        conn.close()
    assert all(p.exists() for p in paths.values())

    summary = store.prune(30)
    assert summary["files"] == 1
    assert not paths[s1].exists()
    surviving = [p for sid, p in paths.items() if sid != s1]
    assert all(p.exists() for p in surviving)


def test_prune_drops_departed_current_rows_and_leads(store, route_bos_raw):
    store.persist(_pf(route_bos_raw))
    conn = db.connect(store.db_path)
    try:
        conn.execute(
            """INSERT INTO explore_leads
               (origin, destination, cabin, month, outbound_date, inbound_date, out_seats,
                in_seats, stay_days, voucher_eligible, computed_at)
               VALUES ('CPH','BOS','AB','2020-01','2020-01-10','2020-01-17',2,2,7,1,'2020-01-01')""",
        )
        conn.execute(
            """INSERT INTO explore_leads
               (origin, destination, cabin, month, outbound_date, inbound_date, out_seats,
                in_seats, stay_days, voucher_eligible, computed_at)
               VALUES ('CPH','BOS','AB','2099-01','2099-01-10','2099-01-17',2,2,7,1,'2020-01-01')""",
        )
        # Simulate a stale award_current row for a flight that has since departed.
        conn.execute(
            """INSERT INTO award_current
               (flight_key, origin, destination, direction, flight_date, cabin, seats,
                seats_total, is_sas_operated, first_seen_at, last_seen_at)
               VALUES ('dead','CPH','BOS','outbound','2020-01-10','AB',2,2,1,'x','x')""",
        )
        before = conn.execute("SELECT COUNT(*) FROM award_current").fetchone()[0]
    finally:
        conn.close()

    summary = store.prune(30)
    assert summary["current_departed"] == 1
    assert summary["leads_departed"] == 1

    conn = db.connect(store.db_path)
    try:
        after = conn.execute("SELECT COUNT(*) FROM award_current").fetchone()[0]
        months = [r["month"] for r in conn.execute("SELECT month FROM explore_leads")]
    finally:
        conn.close()
    assert after == before - 1                  # only the departed row went
    assert months == ["2099-01"]
