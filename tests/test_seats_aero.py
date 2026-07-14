"""seats.aero fallback provider: parser mapping, pagination, budget accounting, and the
provider-switch contract (Search works unchanged on top of it).

The fixture is authored from the Partner API docs — the API isn't live-verified until Kevin
subscribes — so these tests pin OUR mapping decisions, and the fixture doubles as the contract
to re-check against real responses later.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app import db
from app.fetch.budget import Budget, BudgetExceeded
from app.fetch.ratelimit import RateLimiter
from app.providers.base import SCOPE_NETWORK, SCOPE_ROUTE, FeedParseError, ProviderFetch
from app.providers.registry import build_provider
from app.providers.sas_direct.provider import SASDirectProvider
from app.providers.seats_aero.parser import parse_search
from app.providers.seats_aero.provider import SeatsAeroProvider
from app.services.search import SearchService
from app.services.snapshots import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sa_raw() -> str:
    return (FIXTURES / "seats_aero_search_cph_bos.json").read_text(encoding="utf-8")


# ---- parser --------------------------------------------------------------------------------


def test_route_scope_splits_directions(sa_raw):
    feed = parse_search(sa_raw, "CPH", "BOS")
    directions = {(f.direction, f.flight_date) for f in feed.flights}
    assert ("outbound", "2026-11-02") in directions
    assert ("inbound", "2026-11-09") in directions
    # Route convention matches the SAS parser: origin/destination name the route, not the leg.
    assert all(f.origin == "CPH" and f.destination == "BOS" for f in feed.flights)
    # Off-route entries (CPH->EWR, OSL->BOS) and other programs are dropped in route scope.
    assert {f.flight_date for f in feed.flights} == {"2026-11-02", "2026-11-04", "2026-11-09"}


def test_cabin_mapping_and_seat_fallback(sa_raw):
    feed = parse_search(sa_raw, "CPH", "BOS")
    by_key = {(f.flight_date, f.cabin): f for f in feed.flights}
    assert by_key[("2026-11-02", "AG")].seats == 5      # Y -> AG
    assert by_key[("2026-11-02", "AB")].seats == 2      # J -> AB
    assert ("2026-11-02", "AP") not in by_key           # W not available
    # RemainingSeats 0 while Available means "count unknown" -> 1, never 0.
    assert by_key[("2026-11-04", "AG")].seats == 1


def test_sas_operated_only_when_airlines_exactly_sk(sa_raw):
    feed = parse_search(sa_raw, "CPH", "BOS")
    by_key = {(f.flight_date, f.cabin): f for f in feed.flights}
    assert by_key[("2026-11-02", "AB")].is_sas_operated is True     # "SK"
    assert by_key[("2026-11-04", "AB")].is_sas_operated is False    # "SK, KL" — partner metal


def test_other_mileage_programs_are_filtered(sa_raw):
    feed = parse_search(sa_raw, "CPH", "BOS")
    # The aeroplan CPH->BOS entry (9 economy seats) must not leak into 2026-11-02.
    assert all(
        not (f.flight_date == "2026-11-02" and f.cabin == "AG" and f.seats == 9)
        for f in feed.flights
    )


def test_network_scope_keeps_only_origin_outbound(sa_raw):
    feed = parse_search(sa_raw, "CPH")
    assert {f.destination for f in feed.flights} == {"BOS", "EWR"}   # OSL->BOS dropped
    assert all(f.direction == "outbound" for f in feed.flights)
    # F-only cabins are skipped (no EuroBonus F awards) but the Y seats on that entry survive.
    ewr = [f for f in feed.flights if f.destination == "EWR"]
    assert {f.cabin for f in ewr} == {"AG"}
    assert {d.code for d in feed.destinations} == {"BOS", "EWR"}


def test_parse_errors_are_feed_parse_errors():
    with pytest.raises(FeedParseError):
        parse_search({"nope": []}, "CPH", "BOS")
    with pytest.raises(FeedParseError):
        parse_search({"data": [{"Date": "2026-11-02"}]}, "CPH", "BOS")  # missing Route


# ---- provider (mocked transport) -----------------------------------------------------------


def _mock_provider(tmp_db, pages: list[dict], daily_limit: int = 50) -> tuple[SeatsAeroProvider, list]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[min(len(seen) - 1, len(pages) - 1)])

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://seats.aero/partnerapi",
    )
    provider = SeatsAeroProvider(
        "test-key", RateLimiter(0, 0), Budget(tmp_db, daily_limit), client=client,
    )
    return provider, seen


async def test_fetch_route_parses_and_records_budget(tmp_db, sa_raw):
    provider, seen = _mock_provider(tmp_db, [json.loads(sa_raw)])
    pf = await provider.fetch(SCOPE_ROUTE, "CPH", "BOS")
    assert isinstance(pf, ProviderFetch)
    assert (pf.scope, pf.origin, pf.destination, pf.status) == ("route", "CPH", "BOS", "ok")
    assert {f.direction for f in pf.feed.flights} == {"outbound", "inbound"}
    # Raw text round-trips through the parser (snapshot re-parse contract).
    assert len(parse_search(pf.raw_text, "CPH", "BOS").flights) == len(pf.feed.flights)
    # Comma-list request shape: one call covers both directions.
    assert len(seen) == 1
    assert seen[0].url.params["origin_airport"] == "CPH,BOS"
    assert seen[0].url.params["destination_airport"] == "CPH,BOS"
    assert seen[0].headers["Partner-Authorization"] == "test-key"
    # Budget audit: one ok call under the provider's own name.
    assert Budget(tmp_db, 50).used() == 1


async def test_fetch_paginates_until_has_more_false(tmp_db, sa_raw):
    page1 = {"data": json.loads(sa_raw)["data"][:2], "hasMore": True, "cursor": "abc"}
    page2 = {"data": json.loads(sa_raw)["data"][2:], "hasMore": False, "cursor": None}
    provider, seen = _mock_provider(tmp_db, [page1, page2])
    pf = await provider.fetch(SCOPE_ROUTE, "CPH", "BOS")
    assert len(seen) == 2
    assert seen[1].url.params["cursor"] == "abc"
    assert {f.direction for f in pf.feed.flights} == {"outbound", "inbound"}
    assert Budget(tmp_db, 50).used() == 2


async def test_fetch_respects_daily_budget(tmp_db, sa_raw):
    provider, _ = _mock_provider(tmp_db, [json.loads(sa_raw)], daily_limit=0)
    with pytest.raises(BudgetExceeded):
        await provider.fetch(SCOPE_NETWORK, "CPH")
    assert Budget(tmp_db, 0).used() == 0    # blocked before any HTTP call


async def test_http_error_is_recorded_as_failed_call(tmp_db):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://seats.aero/partnerapi",
    )
    provider = SeatsAeroProvider("k", RateLimiter(0, 0), Budget(tmp_db, 50), client=client)
    with pytest.raises(httpx.HTTPStatusError):
        await provider.fetch(SCOPE_ROUTE, "CPH", "BOS")
    assert Budget(tmp_db, 50).used() == 1    # the failed attempt still counts


# ---- config switch + search-unchanged contract ---------------------------------------------


def _settings(**overrides):
    from app.config import Settings

    return Settings(_env_file=None, **overrides)


def test_registry_builds_provider_from_config(tmp_path):
    sas = build_provider(_settings(data_dir=tmp_path), fetcher=None)
    assert isinstance(sas, SASDirectProvider)
    sa = build_provider(
        _settings(data_dir=tmp_path, provider="seats_aero", seats_aero_api_key="k"), fetcher=None,
    )
    assert isinstance(sa, SeatsAeroProvider)
    with pytest.raises(ValueError, match="AF_SEATS_AERO_API_KEY"):
        build_provider(_settings(data_dir=tmp_path, provider="seats_aero"), fetcher=None)
    with pytest.raises(ValueError, match="unknown AF_PROVIDER"):
        build_provider(_settings(data_dir=tmp_path, provider="magic"), fetcher=None)


async def test_search_service_works_unchanged_on_seats_aero(tmp_db, tmp_path, zones, sa_raw):
    """The Phase 4 gate: switching providers leaves the search path working as-is.

    The realistic switch happens on an installed app: the airports catalog (country names ->
    zone pricing) was already filled by earlier SAS fetches. Seed that state, then verify the
    codes-only seats.aero upsert doesn't blank it out.
    """
    provider, _ = _mock_provider(tmp_db, [json.loads(sa_raw)])
    store = SnapshotStore(tmp_db, tmp_path / "snaps")
    store.seed_home_airports(["CPH"])
    conn = db.connect(tmp_db)
    try:
        conn.execute(
            """INSERT INTO airports (code, city_name, country_name, updated_at)
               VALUES ('BOS', 'Boston', 'United States of America', 'x')""",
        )
    finally:
        conn.close()
    search = SearchService(provider, store, zones, snapshot_ttl_s=900)
    result = await search.search(
        "CPH", "BOS", trip_type="RT",
        out_from="2026-11-01", out_to="2026-11-30",
        ret_from="2026-11-01", ret_to="2026-11-30",
        min_stay_days=3, max_stay_days=14,
    )
    assert result.source == "live"
    assert result.trips, "expected paired round-trips from the seats.aero feed"
    trip = result.trips[0].trip
    assert (trip.outbound_date, trip.inbound_date) in {
        ("2026-11-02", "2026-11-09"), ("2026-11-04", "2026-11-09"),
    }
    assert result.trips[0].price.points_total == 160_000   # zone pricing unchanged
    # The codes-only catalog upsert preserved the SAS-provided country (COALESCE contract).
    assert store.country_for("BOS") == "United States of America"
    # And the snapshot persisted through the same store: a second search hits the cache.
    again = await search.search(
        "CPH", "BOS", trip_type="RT",
        out_from="2026-11-01", out_to="2026-11-30",
        ret_from="2026-11-01", ret_to="2026-11-30",
        min_stay_days=3, max_stay_days=14,
    )
    assert again.source == "cache"
