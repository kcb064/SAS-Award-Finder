"""Search orchestration: fresh route snapshot -> round-trip (or one-way) pairing -> pricing.

Uses a cached route snapshot when one is fresh (within the TTL), otherwise fetches live through the
provider and persists it. Returns priced `TripOption`s plus metadata about data freshness so the UI
can show a staleness badge.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import TripOption
from app.providers.base import SCOPE_ROUTE, AwardProvider
from app.services import trips as trips_svc
from app.services.snapshots import SnapshotStore
from app.services.value import TripPrice, TripValue, TripValueService, ZoneTable, price_trip


@dataclass(slots=True)
class PricedTrip:
    trip: TripOption
    price: TripPrice
    value: TripValue | None = None    # cash/cpp side (Phase 4); None when no value service wired


@dataclass(slots=True)
class SearchResult:
    origin: str
    destination: str
    trip_type: str
    trips: list[PricedTrip]
    source: str                 # 'cache' | 'live'
    fetched_at: str | None
    snapshot_age_s: float | None


class SearchService:
    def __init__(
        self,
        provider: AwardProvider,
        store: SnapshotStore,
        zones: ZoneTable,
        *,
        snapshot_ttl_s: int,
        values: TripValueService | None = None,
    ) -> None:
        self._provider = provider
        self._store = store
        self._zones = zones
        self._ttl_s = snapshot_ttl_s
        self._values = values

    async def _route_flights(self, origin: str, destination: str) -> tuple[list, str, str | None]:
        snap = self._store.latest_snapshot(origin, SCOPE_ROUTE, destination)
        if self._store.is_fresh(snap, self._ttl_s):
            flights = self._store.flights_by_snapshot(snap["id"])
            return flights, "cache", snap["fetched_at"]
        pf = await self._provider.fetch(SCOPE_ROUTE, origin, destination)
        self._store.persist(pf)
        return list(pf.feed.flights), "live", datetime.now(timezone.utc).isoformat()

    async def search(
        self,
        origin: str,
        destination: str,
        *,
        trip_type: str = "RT",
        cabin: str | None = None,
        out_from: str | None = None,
        out_to: str | None = None,
        ret_from: str | None = None,
        ret_to: str | None = None,
        min_stay_days: int = 2,
        max_stay_days: int = 30,
        min_seats: int = 1,
        collapse: bool = True,
    ) -> SearchResult:
        origin = origin.upper()
        destination = destination.upper()
        flights, source, fetched_at = await self._route_flights(origin, destination)

        if trip_type == "OW":
            options = trips_svc.one_way_options(
                flights, cabin=cabin, out_from=out_from, out_to=out_to, min_seats=min_seats,
            )
        else:
            options = trips_svc.pair_round_trips(
                flights, cabin=cabin, out_from=out_from, out_to=out_to,
                ret_from=ret_from, ret_to=ret_to,
                min_stay_days=min_stay_days, max_stay_days=max_stay_days, min_seats=min_seats,
            )
            if collapse:
                options = trips_svc.best_round_trip_per_outbound(options)

        origin_country = self._store.country_for(origin)
        dest_country = self._store.country_for(destination)
        priced = []
        for opt in options:
            price = price_trip(
                self._zones, opt, origin_country=origin_country, dest_country=dest_country,
            )
            value = None
            if self._values is not None:
                value = self._values.trip_value(
                    opt, price, origin_country=origin_country, dest_country=dest_country,
                )
            priced.append(PricedTrip(trip=opt, price=price, value=value))

        age_s: float | None = None
        if fetched_at:
            dt = datetime.fromisoformat(fetched_at)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - dt).total_seconds()

        return SearchResult(
            origin=origin, destination=destination, trip_type=trip_type,
            trips=priced, source=source, fetched_at=fetched_at, snapshot_age_s=age_s,
        )
