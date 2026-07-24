"""SkyTeam discovery: live seats.aero searches for partner award space bookable with EuroBonus
points. Runs ALONGSIDE the sas_direct pipeline — nothing here touches snapshots, watches, or the
points table. Results are fetched, filtered, rendered, and forgotten (seats.aero itself is the
cache), so there is no schema for them; pricing shown is seats.aero's own MileageCost, never a
zone estimate (the EuroBonus zone table doesn't price partner metal).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.providers.seats_aero.parser import SkyTeamRow, parse_partner_rows
from app.providers.seats_aero.provider import SeatsAeroProvider
from app.services.snapshots import SnapshotStore
from app.services.value import ZoneTable

# Cap on how many destination codes one region expands into: the comma list rides in the query
# string, and seats.aero's limits on it are undocumented. Alphabetical truncation is honest
# enough for a discovery view; the user can always name airports explicitly.
MAX_REGION_AIRPORTS = 30


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

    def region_names(self) -> list[str]:
        return self._zones.zone_names

    def expand_region(self, region: str) -> list[str]:
        """Region -> destination airport codes, via the SAS catalog's country names.

        Only airports the catalog knows can resolve — run a network refresh first if the list
        comes up empty on a fresh install.
        """
        region = region.strip().upper()
        codes = sorted(
            d["code"]
            for d in self._store.list_destinations()
            if self._zones.zone_for(d["code"], d["country_name"]) == region
        )
        if not codes:
            raise ValueError(
                f"no known airports in region {region} — refresh the network catalog first"
            )
        return codes[:MAX_REGION_AIRPORTS]

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
        today = date.today()
        start = max(date_from or "", today.isoformat()) or today.isoformat()
        end = date_to or (
            date.fromisoformat(start) + timedelta(days=self._default_horizon_days)
        ).isoformat()
        if end < start:
            end = start

        if destinations is None and region:
            destinations = self.expand_region(region)
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
            origins=[o.upper() for o in origins],
            destinations=destinations,
            region=region,
            date_from=start,
            date_to=end,
        )

    async def aclose(self) -> None:
        await self._provider.aclose()
