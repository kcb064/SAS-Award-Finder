"""Explore: ranked trip suggestions Kevin didn't think to search for.

Two layers, matching what the SAS feed actually offers (see docs/api-notes.md):

1. **Destination overview — free.** The outbound-only NETWORK snapshot (refreshed on its own
   schedule) already covers every destination. Aggregating it per destination x cabin gives an
   availability picture — days with seats, days with >=2 seats (voucher potential) — at zero
   request cost. Ranked by `interest x availability_score`.

2. **Round-trip leads — budgeted.** Confirming a round trip needs the ROUTE feed (inbound is only
   populated when destination-scoped), i.e. one request per destination. The sweeper walks the
   overview ranking with a hard per-run budget, fetches route feeds for the stalest interesting
   destinations, and stores the best round-trip per (month, cabin) in `explore_leads`. The UI only
   ever renders cached leads; the nightly job and per-destination Refresh buttons fill them.

Lead identity is (origin, destination, cabin, month-of-outbound). Within a bucket every round trip
costs the same points (zone pricing), so "best" means most bookable: voucher-eligible first, then
the most seats on the thinner leg, then the earliest departure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app import db
from app.fetch.budget import BudgetExceeded
from app.models import CABINS, AwardFlight, TripOption
from app.providers.base import SCOPE_NETWORK, SCOPE_ROUTE, AwardProvider
from app.services import trips as trips_svc
from app.services.notify import AlertStore, ops_alert
from app.services.snapshots import SnapshotStore
from app.services.value import ZoneTable, price_trip

log = logging.getLogger("award_finder.explore")

# Availability scoring: premium cabins are what points are for, so their days count more; days
# with >=2 seats score extra (2-for-1 voucher potential). Day counts are capped so a full year of
# economy can't drown one month of business.
CABIN_WEIGHT = {"AG": 1.0, "AP": 2.0, "AB": 4.0}
DAYS_CAP = 90

MAX_INTEREST = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- pure: scoring + lead computation ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class CabinAvailability:
    days: int                 # dates with >=1 award seat in this cabin
    pair_days: int            # dates with >=2 seats (voucher-pair potential)
    max_seats: int


@dataclass(frozen=True, slots=True)
class DestinationOverview:
    """One destination's ranking row, aggregated from the latest network snapshot."""

    code: str
    city_name: str | None
    country_name: str | None
    region: str               # zone-table region (NORTH_AMERICA, ASIA, ...) — the Explore filter
    interest: int
    cabins: dict[str, CabinAvailability]
    first_date: str
    last_date: str
    score: float
    est_points: dict[str, int]            # estimated round-trip points per cabin (zone table)
    route_fetched_at: str | None = None   # latest ok route snapshot (any consumer) — staleness
    leads_computed_at: str | None = None


@dataclass(frozen=True, slots=True)
class ExploreLead:
    """Best round-trip for one (origin, destination, cabin, outbound-month) bucket."""

    origin: str
    destination: str
    cabin: str
    month: str                # YYYY-MM of the outbound date
    outbound_date: str
    inbound_date: str
    out_seats: int
    in_seats: int
    stay_days: int
    points_total: int | None
    taxes_total: float | None
    voucher_eligible: bool
    computed_at: str


def availability_score(cabins: dict[str, CabinAvailability]) -> float:
    score = 0.0
    for cabin, a in cabins.items():
        weight = CABIN_WEIGHT.get(cabin, 1.0)
        score += weight * min(a.days, DAYS_CAP)
        score += weight * 0.5 * min(a.pair_days, DAYS_CAP)
    return score


def est_rt_points(
    zones: ZoneTable,
    origin: str,
    origin_country: str | None,
    destination: str,
    dest_country: str | None,
) -> dict[str, int]:
    """Estimated round-trip points per cabin from the zone table (display hint, not a quote)."""
    est: dict[str, int] = {}
    for cabin in CABINS:
        out = zones.leg_price(
            dep_code=origin, dep_country=origin_country,
            arr_code=destination, arr_country=dest_country, cabin=cabin,
        )
        back = zones.leg_price(
            dep_code=destination, dep_country=dest_country,
            arr_code=origin, arr_country=origin_country, cabin=cabin,
        )
        est[cabin] = out.points + back.points
    return est


def _bookability(t: TripOption, voucher: bool) -> tuple:
    """Sort key for 'best in bucket': voucher-eligible, then seats on the thinner leg, then
    earliest outbound (ascending overall — max via sorted()[0] on the negated fields)."""
    return (
        0 if voucher else 1,
        -min(t.out_seats, t.in_seats or 0),
        t.outbound_date,
        t.cabin,
    )


def compute_leads(
    flights: Iterable[AwardFlight],
    zones: ZoneTable,
    *,
    origin_country: str | None,
    dest_country: str | None,
    min_stay_days: int = 3,
    max_stay_days: int = 14,
    now: str | None = None,
) -> list[ExploreLead]:
    """Best round-trip per (cabin, outbound-month) from one route feed. Pure.

    Months where the outbound has seats but no return pairs within the stay window produce no
    lead — Explore only ever suggests trips that are actually bookable both ways.
    """
    now = now or _now()
    options = trips_svc.pair_round_trips(
        flights, min_stay_days=min_stay_days, max_stay_days=max_stay_days, min_seats=1,
    )
    buckets: dict[tuple[str, str], list[tuple[TripOption, object]]] = {}
    for opt in options:
        price = price_trip(zones, opt, origin_country=origin_country, dest_country=dest_country)
        buckets.setdefault((opt.cabin, opt.outbound_date[:7]), []).append((opt, price))

    leads: list[ExploreLead] = []
    for (cabin, month), priced in sorted(buckets.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        opt, price = min(priced, key=lambda op: _bookability(op[0], op[1].voucher_eligible))
        leads.append(ExploreLead(
            origin=opt.origin, destination=opt.destination, cabin=cabin, month=month,
            outbound_date=opt.outbound_date, inbound_date=opt.inbound_date,
            out_seats=opt.out_seats, in_seats=opt.in_seats, stay_days=opt.stay_days,
            points_total=price.points_total, taxes_total=price.taxes_total,
            voucher_eligible=price.voucher_eligible, computed_at=now,
        ))
    return leads


# ---- store -------------------------------------------------------------------------------


class ExploreStore:
    """DB reads/writes behind the Explore page: overview aggregation, interest, cached leads."""

    def __init__(self, db_path: Path, snapshots: SnapshotStore, zones: ZoneTable) -> None:
        self.db_path = db_path
        self._snapshots = snapshots
        self._zones = zones

    # ---- interest weights ----------------------------------------------------------------

    def set_interest(self, code: str, interest: int) -> None:
        if not 0 <= interest <= MAX_INTEREST:
            raise ValueError(f"interest must be 0..{MAX_INTEREST}, got {interest}")
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE airports SET interest = ?, updated_at = ? WHERE code = ?",
                (interest, _now(), code.upper()),
            )
        finally:
            conn.close()

    # ---- overview --------------------------------------------------------------------------

    def overview(self, origin: str) -> tuple[list[DestinationOverview], dict | None]:
        """Ranked destinations from the latest network snapshot, plus that snapshot's metadata.

        Returns ([], None) when no network snapshot exists yet (fresh install).
        """
        origin = origin.upper()
        snap = self._snapshots.latest_snapshot(origin, SCOPE_NETWORK)
        if snap is None:
            return [], None

        conn = db.connect(self.db_path)
        try:
            agg = conn.execute(
                """SELECT f.destination, f.cabin, COUNT(*) AS days,
                          SUM(CASE WHEN f.seats >= 2 THEN 1 ELSE 0 END) AS pair_days,
                          MAX(f.seats) AS max_seats,
                          MIN(f.flight_date) AS first_date, MAX(f.flight_date) AS last_date
                   FROM award_flights f
                   JOIN airports a ON a.code = f.destination
                   WHERE f.snapshot_id = ? AND f.direction = 'outbound' AND a.is_home = 0
                   GROUP BY f.destination, f.cabin""",
                (snap["id"],),
            ).fetchall()
            meta = {
                r["code"]: dict(r)
                for r in conn.execute(
                    "SELECT code, city_name, country_name, interest FROM airports"
                ).fetchall()
            }
            route_ages = {
                r["destination"]: r["fetched_at"]
                for r in conn.execute(
                    """SELECT destination, MAX(fetched_at) AS fetched_at
                       FROM availability_snapshots
                       WHERE origin = ? AND scope = 'route' AND status = 'ok'
                       GROUP BY destination""",
                    (origin,),
                ).fetchall()
            }
            lead_ages = {
                r["destination"]: r["computed_at"]
                for r in conn.execute(
                    """SELECT destination, MAX(computed_at) AS computed_at
                       FROM explore_leads WHERE origin = ? GROUP BY destination""",
                    (origin,),
                ).fetchall()
            }
        finally:
            conn.close()

        per_dest: dict[str, dict] = {}
        for r in agg:
            d = per_dest.setdefault(
                r["destination"],
                {"cabins": {}, "first": r["first_date"], "last": r["last_date"]},
            )
            d["cabins"][r["cabin"]] = CabinAvailability(
                days=r["days"], pair_days=r["pair_days"], max_seats=r["max_seats"],
            )
            d["first"] = min(d["first"], r["first_date"])
            d["last"] = max(d["last"], r["last_date"])

        origin_country = (meta.get(origin) or {}).get("country_name")
        rows: list[DestinationOverview] = []
        for code, d in per_dest.items():
            info = meta.get(code) or {}
            interest = info.get("interest", 1)
            rows.append(DestinationOverview(
                code=code,
                city_name=info.get("city_name"),
                country_name=info.get("country_name"),
                region=self._zones.zone_for(code, info.get("country_name")),
                interest=interest,
                cabins=d["cabins"],
                first_date=d["first"],
                last_date=d["last"],
                score=interest * availability_score(d["cabins"]),
                est_points=est_rt_points(
                    self._zones, origin, origin_country, code, info.get("country_name"),
                ),
                route_fetched_at=route_ages.get(code),
                leads_computed_at=lead_ages.get(code),
            ))
        rows.sort(key=lambda o: (-o.score, o.code))
        return rows, snap

    # ---- sweep queue -----------------------------------------------------------------------

    def sweep_queue(self, origin: str) -> list[str]:
        """Destinations worth a route fetch, most deserving first: interest > 0 and some outbound
        availability; never-fetched routes first (best score first), then stalest-first."""
        rows, snap = self.overview(origin)
        if snap is None:
            return []
        candidates = [o for o in rows if o.interest > 0 and o.score > 0]
        never = [o for o in candidates if o.route_fetched_at is None]
        seen = [o for o in candidates if o.route_fetched_at is not None]
        never.sort(key=lambda o: (-o.score, o.code))
        seen.sort(key=lambda o: (o.route_fetched_at, -o.score, o.code))
        return [o.code for o in never + seen]

    # ---- leads -----------------------------------------------------------------------------

    def replace_leads(self, origin: str, destination: str, leads: list[ExploreLead]) -> int:
        """Swap a route's cached leads for a fresh set (stale month buckets disappear)."""
        conn = db.connect(self.db_path)
        try:
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM explore_leads WHERE origin = ? AND destination = ?",
                (origin.upper(), destination.upper()),
            )
            conn.executemany(
                """INSERT INTO explore_leads
                   (origin, destination, cabin, month, outbound_date, inbound_date, out_seats,
                    in_seats, stay_days, points_total, taxes_total, voucher_eligible, computed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        l.origin, l.destination, l.cabin, l.month, l.outbound_date,
                        l.inbound_date, l.out_seats, l.in_seats, l.stay_days, l.points_total,
                        l.taxes_total, int(l.voucher_eligible), l.computed_at,
                    )
                    for l in leads
                ],
            )
            conn.execute("COMMIT")
            return len(leads)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def leads_for(self, origin: str) -> dict[str, list[dict]]:
        """Cached leads for one origin, grouped by destination, month/cabin ordered."""
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                """SELECT * FROM explore_leads WHERE origin = ?
                   ORDER BY destination, month, cabin""",
                (origin.upper(),),
            ).fetchall()
        finally:
            conn.close()
        grouped: dict[str, list[dict]] = {}
        for r in rows:
            grouped.setdefault(r["destination"], []).append(dict(r))
        return grouped


# ---- sweeper -----------------------------------------------------------------------------


class ExploreSweeper:
    """Refreshes round-trip leads for the most deserving destinations, within a hard budget.

    One sweep = at most `per_run_budget` destinations per origin. A fresh route snapshot (e.g.
    Kevin just searched the route) is reused instead of re-hitting SAS — and a reused snapshot
    does not consume sweep budget, since the budget exists to bound SAS requests, not work.
    """

    def __init__(
        self,
        provider: AwardProvider,
        snapshots: SnapshotStore,
        store: ExploreStore,
        zones: ZoneTable,
        alerts: AlertStore,
        *,
        snapshot_ttl_s: int,
        per_run_budget: int,
        min_stay_days: int = 3,
        max_stay_days: int = 14,
    ) -> None:
        self._provider = provider
        self._snapshots = snapshots
        self._store = store
        self._zones = zones
        self._alerts = alerts
        self._ttl_s = snapshot_ttl_s
        self._budget = per_run_budget
        self._min_stay = min_stay_days
        self._max_stay = max_stay_days

    async def run_all(self, origins: list[str]) -> dict:
        summary = {"origins": len(origins), "fetched": 0, "cached": 0, "leads": 0, "failed": 0}
        for origin in origins:
            per = await self.run_origin(origin)
            for k in ("fetched", "cached", "leads", "failed"):
                summary[k] += per[k]
        return summary

    async def run_origin(self, origin: str, budget: int | None = None) -> dict:
        """Walk the sweep queue until the fetch budget for this run is spent."""
        origin = origin.upper()
        budget = self._budget if budget is None else budget
        started = _now()
        queue = self._store.sweep_queue(origin)
        summary = {"queued": len(queue), "fetched": 0, "cached": 0, "leads": 0, "failed": 0}
        status = "ok"
        try:
            for destination in queue:
                if summary["fetched"] >= budget:
                    break
                try:
                    leads, was_cached = await self.refresh_destination(origin, destination)
                except BudgetExceeded as exc:  # global daily budget — stop the whole sweep
                    status = "partial"
                    self._alerts.insert(ops_alert(
                        "request budget exhausted",
                        f"Explore sweep stopped at {origin}->{destination}: {exc}",
                    ))
                    log.warning("explore sweep stopped, daily budget exhausted: %s", exc)
                    break
                except Exception:  # noqa: BLE001 — one bad route must not kill the sweep
                    summary["failed"] += 1
                    status = "partial"
                    log.exception("explore refresh failed for %s->%s", origin, destination)
                    continue
                summary["cached" if was_cached else "fetched"] += 1
                summary["leads"] += leads
            return summary
        finally:
            self._record_sweep(origin, status, started, summary)
            log.info("explore sweep %s: %s", origin, summary)

    async def refresh_destination(self, origin: str, destination: str) -> tuple[int, bool]:
        """Refresh one route's leads. Returns (lead count, reused-fresh-snapshot?)."""
        origin, destination = origin.upper(), destination.upper()
        snap = self._snapshots.latest_snapshot(origin, SCOPE_ROUTE, destination)
        if self._snapshots.is_fresh(snap, self._ttl_s):
            flights, cached = self._snapshots.flights_by_snapshot(snap["id"]), True
        else:
            pf = await self._provider.fetch(SCOPE_ROUTE, origin, destination)
            self._snapshots.persist(pf)
            flights, cached = list(pf.feed.flights), False
        leads = compute_leads(
            flights, self._zones,
            origin_country=self._snapshots.country_for(origin),
            dest_country=self._snapshots.country_for(destination),
            min_stay_days=self._min_stay, max_stay_days=self._max_stay,
        )
        self._store.replace_leads(origin, destination, leads)
        return len(leads), cached

    def _record_sweep(self, origin: str, status: str, started: str, summary: dict) -> None:
        conn = db.connect(self._snapshots.db_path)
        try:
            conn.execute(
                """INSERT INTO sweep_runs (kind, origin, destination, status, started_at,
                   finished_at, notes) VALUES ('explore',?,NULL,?,?,?,?)""",
                (origin, status, started, _now(),
                 f"fetched={summary['fetched']} cached={summary['cached']} "
                 f"leads={summary['leads']} failed={summary['failed']} queued={summary['queued']}"),
            )
        finally:
            conn.close()
