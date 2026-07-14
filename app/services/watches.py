"""Watches: CRUD, per-sweep evaluation, and the background sweep runner.

A watch describes what Kevin wants to be told about — a route, cabin, date windows, stay bounds,
seat minimum, and optionally 2-for-1 voucher-hunt mode. The sweep runner refreshes each watched
route (one scoped request covers every watch on that route), per-leg-diffs the result against
`award_current`, then re-pairs trips per watch and compares against the watch's stored state:

- RT watches alert on the TRIP, not the leg: no-bookable-trip -> bookable fires `opened` (naming
  both dates), best points_total dropping fires `price_drop`, bookable -> none fires `closed`
  (only from a fully-ok sweep), and a bookable trip turning voucher-eligible fires `voucher_pair`.
- OW watches keep the simple per-leg behavior: each newly opened qualifying leg fires `opened`
  individually (after a first-run baseline summary, so a new watch doesn't storm).

Evaluation (`evaluate_watch`) is pure — the scenario-table tests drive it directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from app import db
from app.models import CABIN_NAMES, AwardFlight
from app.providers.base import SCOPE_ROUTE, AwardProvider
from app.providers.sas_direct.endpoints import booking_url
from app.services import trips as trips_svc
from app.services.diffing import CurrentLeg, LegDiff, diff_legs
from app.services.notify import AlertDraft, AlertStore, Notifier, ops_alert, today_utc
from app.services.snapshots import SnapshotStore
from app.services.value import TripValueService, ZoneTable, price_trip

log = logging.getLogger("award_finder.watches")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pts(points: int | None) -> str:
    return f"{points:,}" if points is not None else "?"


@dataclass(frozen=True, slots=True)
class Watch:
    id: int
    origin: str
    destination: str
    trip_type: str                    # 'RT' | 'OW'
    date_from: str
    date_to: str
    label: str | None = None
    cabin: str | None = None          # None = any cabin
    return_from: str | None = None
    return_to: str | None = None
    min_stay_days: int = 2
    max_stay_days: int = 30
    min_seats: int = 1
    sas_only: bool = True
    voucher_mode: bool = False
    enabled: bool = True
    # Sweep state (migration 002) — what the previous evaluation concluded.
    last_run_at: str | None = None
    last_status: str | None = None
    consecutive_failures: int = 0
    had_bookable: bool = False
    best_points: int | None = None
    had_voucher: bool = False

    @classmethod
    def from_row(cls, row: Mapping) -> "Watch":
        return cls(
            id=row["id"], label=row["label"], origin=row["origin"],
            destination=row["destination"], cabin=row["cabin"], trip_type=row["trip_type"],
            date_from=row["date_from"], date_to=row["date_to"],
            return_from=row["return_from"], return_to=row["return_to"],
            min_stay_days=row["min_stay_days"], max_stay_days=row["max_stay_days"],
            min_seats=row["min_seats"], sas_only=bool(row["sas_only"]),
            voucher_mode=bool(row["voucher_mode"]), enabled=bool(row["enabled"]),
            last_run_at=row["last_run_at"], last_status=row["last_status"],
            consecutive_failures=row["consecutive_failures"],
            had_bookable=bool(row["had_bookable"]), best_points=row["best_points"],
            had_voucher=bool(row["had_voucher"]),
        )

    @property
    def display(self) -> str:
        arrow = "⇄" if self.trip_type == "RT" else "→"
        route = f"{self.origin}{arrow}{self.destination}"
        return f"{self.label} ({route})" if self.label else route

    @property
    def effective_min_seats(self) -> int:
        """Voucher-hunt mode needs 2 award seats (both legs) for the 2-for-1 to apply."""
        return max(self.min_seats, 2) if self.voucher_mode else self.min_seats


@dataclass(slots=True)
class WatchOutcome:
    """What one sweep concluded for one watch: its new state + the alerts it should raise."""

    bookable: bool
    best_points: int | None
    has_voucher: bool
    alerts: list[AlertDraft] = field(default_factory=list)
    option_count: int = 0


# ---- store -----------------------------------------------------------------------------


class WatchStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def create(
        self,
        *,
        origin: str,
        destination: str,
        trip_type: str = "RT",
        date_from: str,
        date_to: str,
        return_from: str | None = None,
        return_to: str | None = None,
        label: str | None = None,
        cabin: str | None = None,
        min_stay_days: int = 2,
        max_stay_days: int = 30,
        min_seats: int = 1,
        sas_only: bool = True,
        voucher_mode: bool = False,
    ) -> int:
        if trip_type not in ("RT", "OW"):
            raise ValueError(f"trip_type must be RT or OW, got {trip_type!r}")
        if trip_type == "RT" and not (return_from and return_to):
            raise ValueError("a round-trip watch needs a return window (return_from/return_to)")
        if trip_type == "OW":
            return_from = return_to = None
        now = _now()
        conn = db.connect(self.db_path)
        try:
            cur = conn.execute(
                """INSERT INTO watches
                   (label, origin, destination, cabin, trip_type, date_from, date_to,
                    return_from, return_to, min_stay_days, max_stay_days, min_seats,
                    sas_only, voucher_mode, enabled, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    label or None, origin.upper(), destination.upper(), cabin or None, trip_type,
                    date_from, date_to, return_from, return_to, min_stay_days, max_stay_days,
                    min_seats, int(sas_only), int(voucher_mode), now, now,
                ),
            )
            return cur.lastrowid
        finally:
            conn.close()

    def get(self, watch_id: int) -> Watch | None:
        conn = db.connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
            return Watch.from_row(row) if row else None
        finally:
            conn.close()

    def list_all(self) -> list[Watch]:
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM watches ORDER BY enabled DESC, origin, destination, id"
            ).fetchall()
            return [Watch.from_row(r) for r in rows]
        finally:
            conn.close()

    def list_enabled(self) -> list[Watch]:
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM watches WHERE enabled = 1 ORDER BY origin, destination, id"
            ).fetchall()
            return [Watch.from_row(r) for r in rows]
        finally:
            conn.close()

    def set_enabled(self, watch_id: int, enabled: bool) -> None:
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE watches SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), _now(), watch_id),
            )
        finally:
            conn.close()

    def delete(self, watch_id: int) -> None:
        conn = db.connect(self.db_path)
        try:
            conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        finally:
            conn.close()

    def update_state(self, watch_id: int, outcome: WatchOutcome) -> None:
        """Record a successful evaluation: new trip state + reset the failure streak."""
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                """UPDATE watches SET last_run_at=?, last_status='ok', consecutive_failures=0,
                   had_bookable=?, best_points=?, had_voucher=?, updated_at=? WHERE id=?""",
                (
                    _now(), int(outcome.bookable), outcome.best_points,
                    int(outcome.has_voucher), _now(), watch_id,
                ),
            )
        finally:
            conn.close()

    def record_failures(self, watch_ids: Iterable[int]) -> int:
        """Bump the failure streak on each watch after a failed route sweep. Returns the highest
        resulting streak (the runner alerts ops when it crosses the threshold)."""
        conn = db.connect(self.db_path)
        try:
            highest = 0
            for wid in watch_ids:
                conn.execute(
                    """UPDATE watches SET last_run_at=?, last_status='failed',
                       consecutive_failures=consecutive_failures+1, updated_at=? WHERE id=?""",
                    (_now(), _now(), wid),
                )
                row = conn.execute(
                    "SELECT consecutive_failures FROM watches WHERE id=?", (wid,)
                ).fetchone()
                if row:
                    highest = max(highest, row[0])
            return highest
        finally:
            conn.close()


# ---- evaluation (pure) -----------------------------------------------------------------


def _priced_options(
    watch: Watch,
    flights: Iterable[AwardFlight],
    zones: ZoneTable,
    origin_country: str | None,
    dest_country: str | None,
) -> list[tuple]:
    """The watch's qualifying (TripOption, TripPrice) pairs from this sweep's flights."""
    if watch.trip_type == "OW":
        options = trips_svc.one_way_options(
            flights, cabin=watch.cabin, out_from=watch.date_from, out_to=watch.date_to,
            min_seats=watch.effective_min_seats,
        )
    else:
        options = trips_svc.pair_round_trips(
            flights, cabin=watch.cabin, out_from=watch.date_from, out_to=watch.date_to,
            ret_from=watch.return_from, ret_to=watch.return_to,
            min_stay_days=watch.min_stay_days, max_stay_days=watch.max_stay_days,
            min_seats=watch.effective_min_seats,
        )
    if watch.sas_only:
        options = [o for o in options if o.out_sas_operated and (
            o.inbound_date is None or o.in_sas_operated)]
    priced = [
        (opt, price_trip(zones, opt, origin_country=origin_country, dest_country=dest_country))
        for opt in options
    ]
    if watch.voucher_mode:
        priced = [(o, p) for o, p in priced if p.voucher_eligible]
    return priced


def _trip_line(watch: Watch, opt, price, value=None) -> str:
    cabin = CABIN_NAMES.get(opt.cabin, opt.cabin)
    if opt.inbound_date:
        line = (
            f"{watch.origin}⇄{watch.destination} {cabin}: out {opt.outbound_date} / "
            f"back {opt.inbound_date} ({opt.stay_days}d), seats {opt.out_seats}/{opt.in_seats}, "
            f"{_pts(price.points_total)} pts + ${price.taxes_total:,.0f}"
        )
    else:
        line = (
            f"{watch.origin}→{watch.destination} {cabin}: {opt.outbound_date}, "
            f"{opt.out_seats} seat{'s' if opt.out_seats != 1 else ''}, "
            f"{_pts(price.points_total)} pts + ${price.taxes_total:,.0f}"
        )
    if price.voucher_eligible:
        line += f" — 2-for-1 eligible ({_pts(price.points_per_person_voucher)} pts/person)"
    if value is not None and value.cpp is not None:
        approx = "≈" if value.cash_source == "estimate" else ""
        line += f"\nValue: {value.cpp:.2f}¢/pt (cash {approx}${value.cash_total:,.0f})"
        if value.cpp_voucher is not None:
            line += f" — {value.cpp_voucher:.2f}¢/pt with 2-for-1"
    return line


def _trip_alert(
    watch: Watch, kind: str, title: str, opt, price, extra: str = "", value=None,
) -> AlertDraft:
    body = _trip_line(watch, opt, price, value)
    if extra:
        body += f"\n{extra}"
    body += f"\nBook: {booking_url(watch.origin, watch.destination, opt.outbound_date, opt.inbound_date)}"
    dedup = (
        f"{watch.id}|{kind}|{opt.outbound_date}|{opt.inbound_date or '-'}|{opt.cabin}|{today_utc()}"
    )
    return AlertDraft(
        type=kind, dedup_key=dedup, title=title, body=body, watch_id=watch.id,
        outbound_date=opt.outbound_date, inbound_date=opt.inbound_date, cabin=opt.cabin,
    )


def evaluate_watch(
    watch: Watch,
    flights: Iterable[AwardFlight],
    leg_diff: LegDiff,
    zones: ZoneTable,
    *,
    origin_country: str | None = None,
    dest_country: str | None = None,
    sweep_ok: bool = True,
    value_fn=None,
) -> WatchOutcome:
    """Compare this sweep's re-paired trips against the watch's stored state; emit alert drafts.

    Pure: no I/O. The caller persists state and inserts alerts (dedup happens at insert).
    `value_fn(opt, price) -> TripValue | None` optionally adds a cash/cpp line to alert bodies —
    the impure cash lookup stays with the caller.
    """
    flights = list(flights)
    priced = _priced_options(watch, flights, zones, origin_country, dest_country)

    def _val(opt, price):
        return value_fn(opt, price) if value_fn is not None else None
    bookable = bool(priced)
    best = min(priced, key=lambda op: (op[1].points_total, op[0].outbound_date), default=None)
    best_points = best[1].points_total if best else None
    voucher_priced = [(o, p) for o, p in priced if p.voucher_eligible]
    has_voucher = bool(voucher_priced)

    outcome = WatchOutcome(
        bookable=bookable, best_points=best_points, has_voucher=has_voucher,
        option_count=len(priced),
    )
    first_run = watch.last_run_at is None

    if watch.trip_type == "RT" or first_run or leg_diff.baseline:
        # Trip-level transitions. (OW watches also take this path on their first run / a baseline
        # sweep, so a brand-new watch sends ONE summary instead of a per-date storm.)
        if bookable and not watch.had_bookable:
            kind = "voucher_pair" if watch.voucher_mode else "opened"
            emoji = "🎟️" if watch.voucher_mode else "✅"
            extra = (
                f"{len(priced)} option(s) in your windows." if len(priced) > 1 else ""
            )
            outcome.alerts.append(_trip_alert(
                watch, kind, f"{emoji} {watch.display}: award space open", best[0], best[1], extra,
                value=_val(best[0], best[1]),
            ))
        elif bookable and watch.had_bookable and watch.best_points is not None \
                and best_points is not None and best_points < watch.best_points:
            outcome.alerts.append(_trip_alert(
                watch, "price_drop",
                f"📉 {watch.display}: cheaper award trip ({_pts(best_points)} pts, "
                f"was {_pts(watch.best_points)})",
                best[0], best[1], value=_val(best[0], best[1]),
            ))
        # Voucher transition on ALREADY-bookable space only — a fresh `opened` alert above
        # already carries the 2-for-1 line, so firing both would be noise.
        if bookable and watch.had_bookable and has_voucher and not watch.had_voucher \
                and not watch.voucher_mode and watch.trip_type == "RT":
            vo, vp = min(voucher_priced, key=lambda op: (op[1].points_total, op[0].outbound_date))
            outcome.alerts.append(_trip_alert(
                watch, "voucher_pair", f"🎟️ {watch.display}: 2-for-1 voucher trip available",
                vo, vp, value=_val(vo, vp),
            ))
    else:
        # OW steady state: the plan's simple per-leg behavior — each newly opened qualifying
        # leg alerts individually (dedup per date+cabin+day).
        cabins = (watch.cabin,) if watch.cabin else None
        for f in leg_diff.opened:
            if f.direction != "outbound":
                continue
            if cabins and f.cabin not in cabins:
                continue
            if f.seats < watch.effective_min_seats:
                continue
            if watch.sas_only and not f.is_sas_operated:
                continue
            if f.flight_date < watch.date_from or f.flight_date > watch.date_to:
                continue
            opt_price = [
                (o, p) for o, p in priced
                if o.outbound_date == f.flight_date and o.cabin == f.cabin
            ]
            if not opt_price:
                continue
            o, p = opt_price[0]
            outcome.alerts.append(_trip_alert(
                watch, "opened", f"✅ {watch.display}: new award date {f.flight_date}", o, p,
                value=_val(o, p),
            ))
        if bookable and not watch.had_bookable and not outcome.alerts:
            # The leg diff can be empty even though bookability flipped (e.g. a Search already
            # persisted this snapshot, so award_current was updated before we diffed). Fall back
            # to one summary alert so the transition is never silent.
            outcome.alerts.append(_trip_alert(
                watch, "opened", f"✅ {watch.display}: award space open", best[0], best[1],
                value=_val(best[0], best[1]),
            ))

    if watch.had_bookable and not bookable and sweep_ok:
        outcome.alerts.append(AlertDraft(
            type="closed",
            dedup_key=f"{watch.id}|closed|{today_utc()}",
            title=f"⛔ {watch.display}: award space gone",
            body=(
                f"No bookable {'round-trip' if watch.trip_type == 'RT' else 'flight'} is left in "
                f"your windows ({watch.date_from}..{watch.date_to}"
                + (f", return {watch.return_from}..{watch.return_to}" if watch.return_from else "")
                + ")."
            ),
            watch_id=watch.id,
        ))
    return outcome


# ---- runner ----------------------------------------------------------------------------


class WatchRunner:
    """Sweeps watched routes: one scoped fetch per route covers every watch on it."""

    def __init__(
        self,
        provider: AwardProvider,
        snapshots: SnapshotStore,
        watches: WatchStore,
        alerts: AlertStore,
        notifier: Notifier,
        zones: ZoneTable,
        *,
        snapshot_ttl_s: int,
        ops_failure_threshold: int = 3,
        values: TripValueService | None = None,
    ) -> None:
        self._provider = provider
        self._snapshots = snapshots
        self._watches = watches
        self._alerts = alerts
        self._notifier = notifier
        self._zones = zones
        self._ttl_s = snapshot_ttl_s
        self._ops_threshold = ops_failure_threshold
        self._values = values

    async def run_all(self) -> dict:
        """Sweep every enabled watch (grouped by route), then push pending alerts."""
        watches = self._watches.list_enabled()
        routes: dict[tuple[str, str], list[Watch]] = {}
        for w in watches:
            routes.setdefault((w.origin, w.destination), []).append(w)

        summary = {"routes": len(routes), "watches": len(watches), "ok": 0, "failed": 0,
                   "alerts": 0}
        for (origin, destination), route_watches in routes.items():
            try:
                new_alerts = await self.run_route(origin, destination, route_watches)
                summary["ok"] += 1
                summary["alerts"] += new_alerts
            except Exception:  # noqa: BLE001 — one bad route must not stop the rest
                summary["failed"] += 1
                log.exception("watch sweep failed for %s->%s", origin, destination)
        delivered = await self._notifier.deliver_pending()
        summary["delivered"] = delivered
        log.info("watch sweep done: %s", summary)
        return summary

    async def run_watch(self, watch_id: int) -> int:
        """Run one watch immediately (the UI's 'check now' button). Returns new alert count."""
        watch = self._watches.get(watch_id)
        if watch is None:
            raise ValueError(f"watch {watch_id} not found")
        count = await self.run_route(watch.origin, watch.destination, [watch])
        await self._notifier.deliver_pending()
        return count

    async def run_route(
        self, origin: str, destination: str, route_watches: list[Watch]
    ) -> int:
        """Fetch/diff one route and evaluate its watches. Returns newly inserted alert count."""
        started = _now()
        note = None
        status = "failed"
        new_alerts = 0
        try:
            flights, pf, cached = await self._route_flights(origin, destination)
            # Read the previous state BEFORE persisting — persist() updates award_current,
            # and diffing the sweep against itself would show no changes.
            prev = [
                CurrentLeg.from_row(r)
                for r in self._snapshots.current_route_state(origin, destination)
            ]
            leg_diff = diff_legs(prev, flights, sweep_ok=True)
            if pf is not None:
                self._snapshots.persist(pf)
            self._snapshots.prune_current_route(
                origin, destination, {f.key for f in flights}
            )
            origin_country = self._snapshots.country_for(origin)
            dest_country = self._snapshots.country_for(destination)
            value_fn = None
            if self._values is not None:
                value_fn = lambda o, p: self._values.trip_value(  # noqa: E731
                    o, p, origin_country=origin_country, dest_country=dest_country,
                )
            for watch in route_watches:
                outcome = evaluate_watch(
                    watch, flights, leg_diff, self._zones,
                    origin_country=origin_country, dest_country=dest_country, sweep_ok=True,
                    value_fn=value_fn,
                )
                for draft in outcome.alerts:
                    if self._alerts.insert(draft) is not None:
                        new_alerts += 1
                self._watches.update_state(watch.id, outcome)
            status = "ok"
            note = f"watches={len(route_watches)} cached={cached} alerts={new_alerts}"
            return new_alerts
        except Exception as exc:
            note = f"{type(exc).__name__}: {exc}"
            self._handle_failure(origin, destination, route_watches, exc)
            raise
        finally:
            self._record_sweep(origin, destination, status, started, note)

    async def _route_flights(self, origin: str, destination: str):
        """This route's current flights: (flights, unpersisted ProviderFetch | None, cached).

        Reuses a snapshot within the TTL (e.g. Kevin just searched the route) instead of
        re-hitting SAS. A live fetch is returned unpersisted — run_route persists it after
        reading the previous `award_current` state.
        """
        snap = self._snapshots.latest_snapshot(origin, SCOPE_ROUTE, destination)
        if self._snapshots.is_fresh(snap, self._ttl_s):
            return self._snapshots.flights_by_snapshot(snap["id"]), None, True
        pf = await self._provider.fetch(SCOPE_ROUTE, origin, destination)
        return list(pf.feed.flights), pf, False

    def _handle_failure(
        self, origin: str, destination: str, route_watches: list[Watch], exc: Exception
    ) -> None:
        from app.fetch.budget import BudgetExceeded
        from app.providers.sas_direct.parser import FeedParseError

        if isinstance(exc, BudgetExceeded):
            # Not a provider-health problem — don't grow the failure streak, but do tell Kevin
            # once a day that watches are being skipped.
            self._alerts.insert(ops_alert(
                "request budget exhausted",
                f"Watch sweeps are paused for today: {exc}",
            ))
            return
        if isinstance(exc, FeedParseError):
            # Format drift must never read as "no availability" — alert immediately.
            self._alerts.insert(ops_alert(
                "provider feed format changed",
                f"Parsing the {origin}->{destination} feed failed: {exc}. "
                "Raw snapshots are kept on disk for re-parsing once the parser is fixed.",
            ))
        streak = self._watches.record_failures([w.id for w in route_watches])
        if streak == self._ops_threshold:
            self._alerts.insert(ops_alert(
                f"provider unhealthy ({origin}->{destination})",
                f"{streak} consecutive sweep failures for {origin}->{destination}. "
                f"Last error: {type(exc).__name__}: {exc}. The browser session may need "
                "re-warming; check the Status page.",
            ))

    def _record_sweep(
        self, origin: str, destination: str, status: str, started: str, note: str | None
    ) -> None:
        conn = db.connect(self._snapshots.db_path)
        try:
            conn.execute(
                """INSERT INTO sweep_runs (kind, origin, destination, status, started_at,
                   finished_at, notes) VALUES ('watch',?,?,?,?,?,?)""",
                (origin, destination, status, started, _now(), note),
            )
        finally:
            conn.close()
