"""Watch evaluation (pure scenario tests), store CRUD, and the sweep runner end-to-end."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import DestinationInfo, ParsedFeed
from app.providers.base import ProviderFetch
from app.services.diffing import LegDiff
from app.services.notify import AlertStore, Notifier
from app.services.snapshots import SnapshotStore
from app.services.watches import Watch, WatchRunner, WatchStore, evaluate_watch
from tests.conftest import make_flight

EMPTY_DIFF = LegDiff()


def rt_watch(**over) -> Watch:
    """A CPH⇄BOS round-trip watch with sane windows; override fields per scenario."""
    base = dict(
        id=1, origin="CPH", destination="BOS", trip_type="RT",
        date_from="2026-11-01", date_to="2026-11-10",
        return_from="2026-11-05", return_to="2026-11-20",
        min_stay_days=2, max_stay_days=30, min_seats=1,
        last_run_at="2026-07-01T00:00:00+00:00",  # steady state unless a test overrides
    )
    base.update(over)
    return Watch(**base)


OUT_AB = make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=1)
IN_AB = make_flight(direction="inbound", date="2026-11-09", cabin="AB", seats=1)


# ---- RT: the plan's round-trip cases ---------------------------------------------------


def test_rt_outbound_opens_but_no_return_in_window_means_no_alert(zones):
    late_return = make_flight(direction="inbound", date="2026-11-25", cabin="AB", seats=2)
    out = evaluate_watch(rt_watch(), [OUT_AB, late_return], EMPTY_DIFF, zones)
    assert not out.bookable
    assert out.alerts == []


def test_rt_both_legs_open_alert_names_both_dates(zones):
    out = evaluate_watch(rt_watch(), [OUT_AB, IN_AB], EMPTY_DIFF, zones)
    assert out.bookable
    assert len(out.alerts) == 1
    alert = out.alerts[0]
    assert alert.type == "opened"
    assert alert.outbound_date == "2026-11-02"
    assert alert.inbound_date == "2026-11-09"
    assert "2026-11-02" in alert.body and "2026-11-09" in alert.body
    # Deep link into the pay-with-points flow carries both dates.
    assert "outboundDate=2026-11-02" in alert.body
    assert "inboundDate=2026-11-09" in alert.body
    # No value service wired -> no Value line (and never a crash).
    assert "Value:" not in alert.body


def test_alert_body_carries_cpp_when_value_fn_wired(zones, tmp_db):
    from app.services.value import CashFareStore, TripValueService

    values = TripValueService(CashFareStore(tmp_db), zones)
    value_fn = lambda o, p: values.trip_value(  # noqa: E731 — mirrors the runner's wiring
        o, p, origin_country="Denmark", dest_country="United States of America",
    )
    out = evaluate_watch(
        rt_watch(), [OUT_AB, IN_AB], EMPTY_DIFF, zones,
        origin_country="Denmark", dest_country="United States of America", value_fn=value_fn,
    )
    body = out.alerts[0].body
    # Zone estimate: (2800-180)*100/160000 = 1.64 cents/point, flagged as approximate.
    assert "Value: 1.64¢/pt (cash ≈$2,800)" in body


def test_rt_no_realert_while_still_bookable(zones):
    watch = rt_watch(had_bookable=True, best_points=64000)
    out = evaluate_watch(watch, [OUT_AB, IN_AB], EMPTY_DIFF, zones)
    assert out.bookable
    assert out.alerts == []


def test_rt_price_drop_when_cheaper_cabin_opens(zones):
    watch = rt_watch(had_bookable=True, best_points=64000)  # was Business-only (2x32k)
    econ_out = make_flight(direction="outbound", date="2026-11-02", cabin="AG", seats=1)
    econ_in = make_flight(direction="inbound", date="2026-11-09", cabin="AG", seats=1)
    out = evaluate_watch(watch, [OUT_AB, IN_AB, econ_out, econ_in], EMPTY_DIFF, zones)
    assert out.best_points == 32000  # 2x16k Economy
    assert [a.type for a in out.alerts] == ["price_drop"]
    assert "32,000" in out.alerts[0].title and "64,000" in out.alerts[0].title


def test_rt_closed_only_from_fully_ok_sweep(zones):
    watch = rt_watch(had_bookable=True, best_points=64000)
    ok = evaluate_watch(watch, [], EMPTY_DIFF, zones, sweep_ok=True)
    assert [a.type for a in ok.alerts] == ["closed"]
    partial = evaluate_watch(watch, [], EMPTY_DIFF, zones, sweep_ok=False)
    assert partial.alerts == []


def test_rt_voucher_pair_fires_when_trip_turns_voucher_eligible(zones):
    watch = rt_watch(had_bookable=True, best_points=64000, had_voucher=False)
    out2 = make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=2)
    in2 = make_flight(direction="inbound", date="2026-11-09", cabin="AB", seats=2)
    out = evaluate_watch(watch, [out2, in2], EMPTY_DIFF, zones)
    assert out.has_voucher
    assert [a.type for a in out.alerts] == ["voucher_pair"]
    assert "32,000 pts/person" in out.alerts[0].body  # 64k round-trip halved by the 2-for-1


def test_voucher_mode_needs_two_seats_on_both_legs(zones):
    watch = rt_watch(voucher_mode=True)
    one_seat = evaluate_watch(watch, [OUT_AB, IN_AB], EMPTY_DIFF, zones)
    assert not one_seat.bookable  # 1 seat per leg can't take 2 passengers
    out2 = make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=2)
    in2 = make_flight(direction="inbound", date="2026-11-09", cabin="AB", seats=2)
    two_seats = evaluate_watch(watch, [out2, in2], EMPTY_DIFF, zones)
    assert two_seats.bookable
    assert [a.type for a in two_seats.alerts] == ["voucher_pair"]


def test_rt_stay_window_bounds_pairing(zones):
    watch = rt_watch(min_stay_days=10, max_stay_days=20)
    out = evaluate_watch(watch, [OUT_AB, IN_AB], EMPTY_DIFF, zones)  # stay is 7 days
    assert not out.bookable and out.alerts == []


# ---- OW: per-leg behavior ---------------------------------------------------------------


def ow_watch(**over) -> Watch:
    base = dict(
        id=2, origin="CPH", destination="BOS", trip_type="OW",
        date_from="2026-11-01", date_to="2026-11-10",
        last_run_at="2026-07-01T00:00:00+00:00",
    )
    base.update(over)
    return Watch(**base)


def test_ow_first_run_sends_single_summary_not_a_storm(zones):
    flights = [
        make_flight(direction="outbound", date=f"2026-11-0{d}", cabin="AG", seats=3)
        for d in range(2, 8)
    ]
    out = evaluate_watch(ow_watch(last_run_at=None), flights, EMPTY_DIFF, zones)
    assert out.bookable
    assert len(out.alerts) == 1
    assert out.alerts[0].type == "opened"


def test_ow_steady_state_alerts_per_newly_opened_leg(zones):
    known = make_flight(direction="outbound", date="2026-11-02", cabin="AG", seats=3)
    fresh = make_flight(direction="outbound", date="2026-11-05", cabin="AG", seats=2)
    outside = make_flight(direction="outbound", date="2026-12-01", cabin="AG", seats=2)
    diff = LegDiff(opened=[fresh, outside])
    out = evaluate_watch(ow_watch(had_bookable=True), [known, fresh, outside], diff, zones)
    assert len(out.alerts) == 1
    assert out.alerts[0].outbound_date == "2026-11-05"  # the out-of-window date stays quiet
    assert out.alerts[0].inbound_date is None


def test_ow_bookable_flip_with_empty_diff_still_alerts(zones):
    # e.g. a Search already persisted this snapshot, so the leg diff came back empty.
    fresh = make_flight(direction="outbound", date="2026-11-05", cabin="AG", seats=2)
    out = evaluate_watch(ow_watch(had_bookable=False), [fresh], EMPTY_DIFF, zones)
    assert [a.type for a in out.alerts] == ["opened"]


def test_ow_closed_when_last_leg_disappears(zones):
    out = evaluate_watch(ow_watch(had_bookable=True), [], EMPTY_DIFF, zones)
    assert [a.type for a in out.alerts] == ["closed"]


# ---- store CRUD -------------------------------------------------------------------------


def test_watch_store_crud(tmp_db: Path):
    store = WatchStore(tmp_db)
    wid = store.create(
        origin="cph", destination="bos", trip_type="RT",
        date_from="2026-11-01", date_to="2026-11-10",
        return_from="2026-11-05", return_to="2026-11-20", label="Boston",
    )
    w = store.get(wid)
    assert w is not None and w.origin == "CPH" and w.destination == "BOS"
    assert w.enabled and w.last_run_at is None

    store.set_enabled(wid, False)
    assert not store.get(wid).enabled
    assert store.list_enabled() == []
    assert len(store.list_all()) == 1

    store.delete(wid)
    assert store.get(wid) is None


def test_rt_watch_requires_return_window(tmp_db: Path):
    store = WatchStore(tmp_db)
    with pytest.raises(ValueError, match="return window"):
        store.create(
            origin="CPH", destination="BOS", trip_type="RT",
            date_from="2026-11-01", date_to="2026-11-10",
        )


def test_ow_watch_drops_return_window(tmp_db: Path):
    store = WatchStore(tmp_db)
    wid = store.create(
        origin="CPH", destination="BOS", trip_type="OW",
        date_from="2026-11-01", date_to="2026-11-10",
        return_from="2026-11-05", return_to="2026-11-20",
    )
    w = store.get(wid)
    assert w.return_from is None and w.return_to is None


def test_failure_streak_bookkeeping(tmp_db: Path):
    store = WatchStore(tmp_db)
    wid = store.create(
        origin="CPH", destination="BOS", trip_type="OW",
        date_from="2026-11-01", date_to="2026-11-10",
    )
    assert store.record_failures([wid]) == 1
    assert store.record_failures([wid]) == 2
    assert store.get(wid).last_status == "failed"


# ---- runner end-to-end ------------------------------------------------------------------


class FakeProvider:
    name = "fake"
    capabilities = {"route"}

    def __init__(self) -> None:
        self.flights = []
        self.calls = 0
        self.error: Exception | None = None

    async def fetch(self, scope, origin, destination=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        feed = ParsedFeed(
            destinations=[DestinationInfo(code=destination, city_name="Boston",
                                          country_name="United States of America")],
            flights=list(self.flights),
        )
        return ProviderFetch(
            scope=scope, origin=origin, destination=destination, feed=feed,
            raw_text="[]", http_status=200, byte_size=2, duration_ms=1, status="ok",
        )


class RecordingNotifier(Notifier):
    def __init__(self, store: AlertStore) -> None:
        super().__init__(store, ["json://fake"])  # enabled, but _send never touches Apprise
        self.sent: list[tuple[str, str]] = []

    def _send(self, title: str, body: str) -> bool:
        self.sent.append((title, body))
        return True


@pytest.fixture
def runner_env(tmp_db: Path, tmp_path: Path, zones):
    snapshots = SnapshotStore(tmp_db, tmp_path / "snaps")
    watch_store = WatchStore(tmp_db)
    alert_store = AlertStore(tmp_db)
    notifier = RecordingNotifier(alert_store)
    provider = FakeProvider()
    runner = WatchRunner(
        provider, snapshots, watch_store, alert_store, notifier, zones,
        snapshot_ttl_s=0,  # always fetch live so every run is a real sweep
        ops_failure_threshold=3,
    )
    return runner, provider, watch_store, alert_store, notifier


async def test_runner_full_lifecycle(runner_env):
    runner, provider, watch_store, alert_store, notifier = runner_env
    wid = watch_store.create(
        origin="CPH", destination="BOS", trip_type="RT",
        date_from="2026-11-01", date_to="2026-11-10",
        return_from="2026-11-05", return_to="2026-11-20",
    )

    # Sweep 1: both legs available with 2 seats -> one opened alert, pushed.
    provider.flights = [
        make_flight(direction="outbound", date="2026-11-02", cabin="AB", seats=2),
        make_flight(direction="inbound", date="2026-11-09", cabin="AB", seats=2),
    ]
    summary = await runner.run_all()
    assert summary["ok"] == 1 and summary["alerts"] == 1 and summary["delivered"] == 1
    assert len(notifier.sent) == 1
    title, body = notifier.sent[0]
    assert "2026-11-02" in body and "2026-11-09" in body and "flysas.com" in body
    w = watch_store.get(wid)
    # BOS is seeded as United States -> SCANDINAVIA|NORTH_AMERICA Business = 80k per leg.
    assert w.had_bookable and w.best_points == 160000 and w.last_status == "ok"

    # Sweep 2: unchanged -> quiet.
    summary = await runner.run_all()
    assert summary["alerts"] == 0

    # Sweep 3: return leg gone -> a fully-ok sweep emits closed.
    provider.flights = provider.flights[:1]
    summary = await runner.run_all()
    assert summary["alerts"] == 1
    assert any(a["type"] == "closed" for a in alert_store.recent())
    assert not watch_store.get(wid).had_bookable


async def test_runner_failure_streak_raises_ops_alert(runner_env):
    runner, provider, watch_store, alert_store, notifier = runner_env
    watch_store.create(
        origin="CPH", destination="BOS", trip_type="OW",
        date_from="2026-11-01", date_to="2026-11-10",
    )
    provider.error = RuntimeError("cloudflare said no")
    for _ in range(3):
        await runner.run_all()

    ops = [a for a in alert_store.recent() if a["type"] == "ops"]
    assert len(ops) == 1  # fires exactly at the threshold, not on every failure
    assert "3 consecutive" in ops[0]["body"]

    # Recovery: a clean sweep resets the streak and finds the space.
    provider.error = None
    provider.flights = [make_flight(direction="outbound", date="2026-11-03", cabin="AG", seats=1)]
    await runner.run_all()
    w = watch_store.list_all()[0]
    assert w.consecutive_failures == 0 and w.last_status == "ok" and w.had_bookable
