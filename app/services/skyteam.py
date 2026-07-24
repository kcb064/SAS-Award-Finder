"""SkyTeam discovery: live seats.aero searches for partner award space bookable with EuroBonus
points. Runs ALONGSIDE the sas_direct pipeline — nothing here touches snapshots, watches, or the
points table. Results are fetched, filtered, rendered, and forgotten (seats.aero itself is the
cache), so there is no schema for them; pricing shown is seats.aero's own MileageCost, never a
zone estimate (the EuroBonus zone table doesn't price partner metal). The one thing held in
memory is the per-source seats.aero route map that region searches expand against.

Round trips mirror services/trips.py: a second /search covers the return direction
(destinations -> origins, window shifted by the stay bounds), and legs pair per route+cabin when
the stay length lands inside [min_stay_days, max_stay_days]. Pairing happens here rather than in
trips.py because SkyTeamRow carries fields AwardFlight has no room for (partner airlines,
per-program mileage, taxes).
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from app.providers.seats_aero.endpoints import SKYTEAM_SOURCES
from app.providers.seats_aero.parser import SkyTeamRow, parse_partner_rows
from app.providers.seats_aero.provider import SeatsAeroProvider
from app.services.snapshots import SnapshotStore
from app.services.value import ZoneTable

log = logging.getLogger(__name__)

# Cap on how many destination codes one region expands into: the comma list rides in the query
# string, and seats.aero's limits on it are undocumented. Truncation keeps the airports served
# by the most cached routes; the user can always name airports explicitly.
MAX_REGION_AIRPORTS = 30

# How long a fetched seats.aero route map stays fresh. Route maps change on the order of
# schedule seasons, so one budgeted call per source per day is plenty.
ROUTES_TTL_S = 24 * 3600

# seats.aero /routes region vocabulary -> EuroBonus zone (live-verified 2026-07-24). The API
# only knows continents: Scandinavia sits inside "Europe" and the Middle East inside "Asia", so
# SCANDINAVIA/MIDDLE_EAST only match airports the SAS catalog or airport_zones overrides can
# place — for everything else this coarse map decides.
_SA_REGION_TO_ZONE = {
    "Europe": "EUROPE",
    "Asia": "ASIA",
    "North America": "NORTH_AMERICA",
    "South America": "SOUTH_AMERICA",
    "Africa": "AFRICA",
    "Oceania": "OCEANIA",
}


@dataclass(frozen=True, slots=True)
class SkyTeamTrip:
    """One bookable round-trip pairing of two SkyTeamRow legs (same route reversed, same cabin).

    Totals are only summed when they mean something: mileage when BOTH legs carry a price (the
    legs may come from different programs — still indicative, the hint under the table already
    says EuroBonus prices differently), taxes only when both legs quote the same currency.
    """

    out: SkyTeamRow
    ret: SkyTeamRow
    stay_days: int
    mileage_total: int | None
    taxes_total: float | None
    taxes_currency: str | None
    direct: bool | None              # True = both legs direct, False = a leg isn't, None = unknown
    voucher_usable: bool             # both legs SAS-operated with >=2 CONFIRMED seats
    sources: tuple[str, ...]


def _make_trip(out: SkyTeamRow, ret: SkyTeamRow, stay_days: int) -> SkyTeamTrip:
    mileage = (
        out.mileage_cost + ret.mileage_cost
        if out.mileage_cost is not None and ret.mileage_cost is not None else None
    )
    if (out.total_taxes is not None and ret.total_taxes is not None
            and out.taxes_currency == ret.taxes_currency):
        taxes, currency = out.total_taxes + ret.total_taxes, out.taxes_currency
    else:
        taxes, currency = None, None
    if out.direct is False or ret.direct is False:
        direct: bool | None = False
    elif out.direct and ret.direct:
        direct = True
    else:
        direct = None
    return SkyTeamTrip(
        out=out, ret=ret, stay_days=stay_days,
        mileage_total=mileage, taxes_total=taxes, taxes_currency=currency,
        direct=direct,
        voucher_usable=(out.sas_operated and ret.sas_operated
                        and out.seats >= 2 and ret.seats >= 2),
        sources=tuple(dict.fromkeys((out.source, ret.source))),
    )


def _dedupe_legs(rows: list[SkyTeamRow]) -> list[SkyTeamRow]:
    """One row per (date, route, cabin) before pairing — several programs usually report the
    SAME physical award space, and pairing the cross product would multiply identical trips.
    Prefer the SAS-operated report (voucher relevance), then more confirmed seats, then a row
    that carries a mileage price."""
    best: dict[tuple[str, str, str, str], SkyTeamRow] = {}
    for r in rows:
        k = (r.date, r.origin, r.destination, r.cabin)
        cur = best.get(k)
        if cur is None or _leg_rank(r) > _leg_rank(cur):
            best[k] = r
    return list(best.values())


def _leg_rank(r: SkyTeamRow) -> tuple[bool, int, bool]:
    return (r.sas_operated, r.seats, r.mileage_cost is not None)


def _pair_round_trips(
    out_rows: list[SkyTeamRow], ret_rows: list[SkyTeamRow],
    *, min_stay_days: int, max_stay_days: int,
) -> list[SkyTeamTrip]:
    by_route: dict[tuple[str, str, str], list[SkyTeamRow]] = defaultdict(list)
    for r in ret_rows:
        by_route[(r.origin, r.destination, r.cabin)].append(r)
    trips: list[SkyTeamTrip] = []
    for o in out_rows:
        out_day = date.fromisoformat(o.date)
        for r in by_route.get((o.destination, o.origin, o.cabin), ()):
            stay = (date.fromisoformat(r.date) - out_day).days
            if min_stay_days <= stay <= max_stay_days:
                trips.append(_make_trip(o, r, stay))
    trips.sort(key=lambda t: (
        t.out.date, t.ret.date, t.out.origin, t.out.destination, t.out.cabin,
    ))
    return trips


def _best_return_per_outbound(trips: list[SkyTeamTrip]) -> list[SkyTeamTrip]:
    """Collapse to the single best return per (route, outbound date, cabin): the most seats on
    the weaker leg, then the shortest stay — same rule as trips.best_round_trip_per_outbound.
    An unknown count (0) ranks as 1 ("at least one")."""
    best: dict[tuple[str, str, str, str], SkyTeamTrip] = {}
    for t in trips:
        k = (t.out.origin, t.out.destination, t.out.date, t.out.cabin)
        cur = best.get(k)
        if cur is None or _trip_rank(t) > _trip_rank(cur):
            best[k] = t
    return sorted(best.values(), key=lambda t: (
        t.out.date, t.out.origin, t.out.destination, t.out.cabin,
    ))


def _trip_rank(t: SkyTeamTrip) -> tuple[int, int]:
    return (min(t.out.seats or 1, t.ret.seats or 1), -t.stay_days)


@dataclass(slots=True)
class SkyTeamResult:
    rows: list[SkyTeamRow]
    total: int                       # rows (OW) / trips (RT) matched before truncation
    truncated: bool
    origins: list[str]
    destinations: list[str] | None   # resolved list actually sent (None = anywhere)
    region: str | None
    date_from: str
    date_to: str
    trip_type: str = "OW"
    trips: list[SkyTeamTrip] = field(default_factory=list)


class SkyTeamService:
    def __init__(
        self,
        provider: SeatsAeroProvider,
        store: SnapshotStore,
        zones: ZoneTable,
        *,
        default_horizon_days: int,
        sources: tuple[str, ...] | None = None,
        max_rows: int = 500,
    ) -> None:
        self._provider = provider
        self._store = store
        self._zones = zones
        self._default_horizon_days = default_horizon_days
        self._sources = tuple(sources) if sources else None   # None -> parser default
        self._max_rows = max_rows
        # source -> (monotonic fetch time, routes). Kept for the app's lifetime; stale entries
        # are reused if a refetch fails (a day-old route map beats none).
        self._routes_cache: dict[str, tuple[float, list[dict]]] = {}

    def region_names(self) -> list[str]:
        return self._zones.zone_names

    async def _routes(self, source: str) -> list[dict]:
        """The cached seats.aero route map for one source (one budgeted call per TTL)."""
        now = time.monotonic()
        cached = self._routes_cache.get(source)
        if cached and now - cached[0] < ROUTES_TTL_S:
            return cached[1]
        try:
            routes = await self._provider.get_routes(source)
        except httpx.HTTPError as exc:
            # Degrade instead of failing the search: stale map if we have one, else the SAS
            # catalog alone carries the expansion.
            log.warning("seats.aero route map fetch failed for %s: %s", source, exc)
            return cached[1] if cached else []
        self._routes_cache[source] = (now, routes)
        return routes

    def _zone_of(self, code: str, sa_region: str | None, catalog: dict[str, str | None]) -> str | None:
        """Best-effort EuroBonus zone for a route-map destination. Order matters: an explicit
        airport override beats the SAS catalog's country, which beats seats.aero's continent."""
        override = self._zones.airport_override(code)
        if override:
            return override
        country = catalog.get(code)
        if country:
            return self._zones.zone_for(code, country)
        return _SA_REGION_TO_ZONE.get((sa_region or "").strip())

    async def expand_region(self, region: str, origins: list[str]) -> list[str]:
        """Region -> destination airport codes, best-first.

        Candidates are the union of two catalogs: the seats.aero route maps for the configured
        SkyTeam sources (the only markets /search can answer, and the part of the SkyTeam
        network SAS never flies) and the SAS network catalog (which also refines coarse
        continents into SCANDINAVIA/MIDDLE_EAST via country names). Ranked by how many cached
        (origin, source) routes serve each airport — partner-served first — then A-Z, capped
        at MAX_REGION_AIRPORTS.

        When neither catalog knows the region from these origins (e.g. SOUTH_AMERICA from CPH:
        no partner nonstop, and SAS never flies there), fall back to the region's airports
        served from ANYWHERE in the route maps, ranked by how well-served they are overall.
        /search decides whether any of those markets hold cached space — an empty result page
        beats refusing to search.
        """
        region = region.strip().upper()
        if region not in self._zones.zone_names:
            raise ValueError(
                f"unknown region {region} — expected one of: {', '.join(self._zones.zone_names)}"
            )
        origin_set = {o.strip().upper() for o in origins if o.strip()}
        catalog: dict[str, str | None] = {
            d["code"]: d["country_name"] for d in self._store.list_destinations()
        }
        route_maps = [await self._routes(source) for source in self._sources or SKYTEAM_SOURCES]

        def scan(require_origin: bool) -> dict[str, int]:
            scores: dict[str, int] = {}
            for routes in route_maps:
                for r in routes:
                    if require_origin and r.get("OriginAirport") not in origin_set:
                        continue
                    code = str(r.get("DestinationAirport") or "").upper()
                    if not code or code in origin_set:
                        continue
                    if self._zone_of(code, r.get("DestinationRegion"), catalog) != region:
                        continue
                    scores[code] = scores.get(code, 0) + 1
            return scores

        scores = scan(require_origin=True)
        for code, country in catalog.items():
            if code not in origin_set and self._zones.zone_for(code, country) == region:
                scores.setdefault(code, 0)
        if not scores:
            scores = scan(require_origin=False)
        if not scores:
            raise ValueError(
                f"no known airports in region {region} from {', '.join(sorted(origin_set))} — "
                "name destination airports explicitly, or refresh the network catalog"
            )
        ranked = sorted(scores, key=lambda c: (-scores[c], c))
        return ranked[:MAX_REGION_AIRPORTS]

    async def _partner_rows(
        self, origins: list[str], destinations: list[str], start: str, end: str
    ) -> list[SkyTeamRow]:
        entries = await self._provider.search_entries(
            origins, destinations, start_date=start, end_date=end,
        )
        if self._sources:
            return parse_partner_rows({"data": entries}, sources=self._sources)
        return parse_partner_rows({"data": entries})

    async def search(
        self,
        *,
        origins: list[str],
        destinations: list[str] | None = None,
        region: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        cabin: str | None = None,
        min_seats: int = 1,
        sas_only: bool = False,
        direct_only: bool = False,
        trip_type: str = "OW",
        min_stay_days: int = 3,
        max_stay_days: int = 14,
        collapse: bool = True,
    ) -> SkyTeamResult:
        origins = [o.strip().upper() for o in origins if o.strip()]
        today = date.today()
        start = max(date_from or "", today.isoformat()) or today.isoformat()
        end = date_to or (
            date.fromisoformat(start) + timedelta(days=self._default_horizon_days)
        ).isoformat()
        if end < start:
            end = start

        if destinations is None and region:
            destinations = await self.expand_region(region, origins)
        if not destinations:
            # Live-verified: /search silently returns nothing without destination_airport.
            raise ValueError(
                "seats.aero needs destinations — pick a region or name destination airports"
            )

        def keep(r: SkyTeamRow, lo: str, hi: str) -> bool:
            if not (lo <= r.date <= hi):
                return False
            if cabin and r.cabin != cabin:
                return False
            # seats == 0 means "count unknown, at least 1": passes only a min_seats<=1 bar.
            if min_seats > 1 and r.seats < min_seats:
                return False
            if sas_only and not r.sas_operated:
                return False
            if direct_only and r.direct is not True:
                return False
            return True

        out_rows = [
            r for r in await self._partner_rows(origins, destinations, start, end)
            if keep(r, start, end)
        ]

        if trip_type == "RT":
            max_stay_days = max(min_stay_days, max_stay_days)
            # The return window is the outbound window shifted by the stay bounds — a second
            # budgeted /search with the airport lists swapped.
            ret_start = max(
                (date.fromisoformat(start) + timedelta(days=min_stay_days)).isoformat(),
                today.isoformat(),
            )
            ret_end = (date.fromisoformat(end) + timedelta(days=max_stay_days)).isoformat()
            ret_rows = [
                r for r in await self._partner_rows(destinations, origins, ret_start, ret_end)
                if keep(r, ret_start, ret_end)
            ]
            trips = _pair_round_trips(
                _dedupe_legs(out_rows), _dedupe_legs(ret_rows),
                min_stay_days=min_stay_days, max_stay_days=max_stay_days,
            )
            if collapse:
                trips = _best_return_per_outbound(trips)
            return SkyTeamResult(
                rows=[],
                total=len(trips),
                truncated=len(trips) > self._max_rows,
                origins=origins,
                destinations=destinations,
                region=region,
                date_from=start,
                date_to=end,
                trip_type="RT",
                trips=trips[: self._max_rows],
            )

        matched = sorted(
            out_rows, key=lambda r: (r.date, r.origin, r.destination, r.cabin),
        )
        return SkyTeamResult(
            rows=matched[: self._max_rows],
            total=len(matched),
            truncated=len(matched) > self._max_rows,
            origins=origins,
            destinations=destinations,
            region=region,
            date_from=start,
            date_to=end,
        )

    async def aclose(self) -> None:
        await self._provider.aclose()
