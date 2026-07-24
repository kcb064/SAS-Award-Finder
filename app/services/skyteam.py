"""SkyTeam discovery: live seats.aero searches for partner award space bookable with EuroBonus
points. Runs ALONGSIDE the sas_direct pipeline — nothing here touches snapshots, watches, or the
points table. Results are fetched, filtered, rendered, and forgotten (seats.aero itself is the
cache), so there is no schema for them; pricing shown is seats.aero's own MileageCost, never a
zone estimate (the EuroBonus zone table doesn't price partner metal). The one thing held in
memory is the per-source seats.aero route map that region searches expand against.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
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


@dataclass(slots=True)
class SkyTeamResult:
    rows: list[SkyTeamRow]
    total: int                       # rows matched before truncation
    truncated: bool
    origins: list[str]
    destinations: list[str] | None   # resolved list actually sent (None = anywhere)
    region: str | None
    date_from: str
    date_to: str


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
        scores: dict[str, int] = {}
        for source in self._sources or SKYTEAM_SOURCES:
            for r in await self._routes(source):
                if r.get("OriginAirport") not in origin_set:
                    continue
                code = str(r.get("DestinationAirport") or "").upper()
                if not code or code in origin_set:
                    continue
                if self._zone_of(code, r.get("DestinationRegion"), catalog) != region:
                    continue
                scores[code] = scores.get(code, 0) + 1
        for code, country in catalog.items():
            if code not in origin_set and self._zones.zone_for(code, country) == region:
                scores.setdefault(code, 0)
        if not scores:
            raise ValueError(
                f"no known airports in region {region} from {', '.join(sorted(origin_set))} — "
                "name destination airports explicitly, or refresh the network catalog"
            )
        ranked = sorted(scores, key=lambda c: (-scores[c], c))
        return ranked[:MAX_REGION_AIRPORTS]

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

        entries = await self._provider.search_entries(
            origins, destinations, start_date=start, end_date=end,
        )
        if self._sources:
            rows = parse_partner_rows({"data": entries}, sources=self._sources)
        else:
            rows = parse_partner_rows({"data": entries})

        def keep(r: SkyTeamRow) -> bool:
            if not (start <= r.date <= end):
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

        matched = sorted(
            (r for r in rows if keep(r)),
            key=lambda r: (r.date, r.origin, r.destination, r.cabin),
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
