"""SkyTeam tab (Phase 5): partner-row parsing, the live-only SkyTeamService, the second
provider instance, and the provider-scoped budget that lets seats.aero run alongside SAS."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from app import db
from app.fetch.budget import Budget, BudgetExceeded, ProviderCall
from app.fetch.ratelimit import RateLimiter
from app.providers.registry import build_skyteam_provider
from app.providers.seats_aero.parser import parse_partner_rows
from app.providers.seats_aero.provider import SeatsAeroProvider
from app.services.skyteam import SkyTeamService
from app.services.snapshots import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sa_raw() -> str:
    return (FIXTURES / "seats_aero_search_cph_bos.json").read_text(encoding="utf-8")


@pytest.fixture
def live_raw() -> str:
    # Recorded from the REAL Partner API on 2026-07-24 (CPH->BKK, one entry per source).
    return (FIXTURES / "seats_aero_search_live_cph_bkk.json").read_text(encoding="utf-8")


# ---- parse_partner_rows (pinned against the recorded live response) ------------------------


def test_default_sources_are_the_skyteam_programs(live_raw):
    rows = parse_partner_rows(live_raw)
    # flyingblue/delta/virginatlantic kept; aeroplan/etihad/united (non-SkyTeam) dropped.
    assert {r.source for r in rows} == {"flyingblue", "delta", "virginatlantic"}
    assert all(r.origin == "CPH" and r.destination == "BKK" for r in rows)


def test_partner_metal_kept_and_sas_only_on_exact_sk(live_raw):
    rows = parse_partner_rows(live_raw)
    fb_j = next(r for r in rows if r.source == "flyingblue" and r.cabin == "AB")
    assert fb_j.airlines == ("AF", "KL", "SK")
    # SK is bookable here, but seats can't be attributed to SK alone -> never "SAS-operated".
    assert fb_j.sas_operated is False
    assert fb_j.seats == 2
    vn_j = next(r for r in rows if r.source == "virginatlantic" and r.cabin == "AB")
    assert vn_j.airlines == ("VN",)


def test_live_mileage_and_minor_unit_taxes(live_raw):
    rows = parse_partner_rows(live_raw)
    fb_j = next(r for r in rows if r.source == "flyingblue" and r.cabin == "AB")
    assert fb_j.mileage_cost == 97500          # "97500" string -> int
    assert fb_j.total_taxes == 481.0           # 48100 USD cents -> dollars
    assert fb_j.taxes_currency == "USD"
    vn_j = next(r for r in rows if r.source == "virginatlantic" and r.cabin == "AB")
    assert vn_j.total_taxes == 3945.0          # 394500 øre -> DKK
    assert vn_j.taxes_currency == "DKK"


def test_explicit_sources_override(live_raw):
    rows = parse_partner_rows(live_raw, sources=("united",))
    assert {r.source for r in rows} == {"united"}
    # united reports RemainingSeats 0 ("count unknown") — kept as 0 for honest "1+" display,
    # so the voucher badge can insist on >=2 CONFIRMED seats.
    assert {r.seats for r in rows} == {0}


def test_zero_mileage_means_no_figure():
    entry = {
        "Route": {"OriginAirport": "CPH", "DestinationAirport": "BKK", "Source": "flyingblue"},
        "Date": "2026-10-05",
        "JAvailable": True, "JRemainingSeats": 2, "JAirlines": "KL",
        "JMileageCost": "0", "JTotalTaxes": 0, "TaxesCurrency": "USD",
        "JDirect": True,
    }
    row = parse_partner_rows({"data": [entry]})[0]
    assert row.mileage_cost is None            # "0" == not priced, never "free"
    assert row.total_taxes is None
    assert row.direct is True


def test_f_cabin_never_surfaces(live_raw):
    rows = parse_partner_rows(live_raw)
    assert rows and all(r.cabin in {"AG", "AP", "AB"} for r in rows)


# ---- SkyTeamService ------------------------------------------------------------------------


def _service(tmp_db, tmp_path, zones, pages: list[dict], daily_limit: int = 50,
             **svc_kwargs) -> tuple[SkyTeamService, list]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[min(len(seen) - 1, len(pages) - 1)])

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://seats.aero/partnerapi",
    )
    provider = SeatsAeroProvider(
        "test-key", RateLimiter(0, 0),
        Budget(tmp_db, daily_limit, provider="seats_aero"), client=client,
    )
    store = SnapshotStore(tmp_db, tmp_path / "snaps")
    store.seed_home_airports(["CPH"])
    conn = db.connect(tmp_db)
    try:
        conn.executemany(
            "INSERT INTO airports (code, city_name, country_name, updated_at) VALUES (?,?,?,?)",
            [
                ("BKK", "Bangkok", "Thailand", "x"),
                ("NRT", "Tokyo", "Japan", "x"),
                ("BOS", "Boston", "United States of America", "x"),
            ],
        )
    finally:
        conn.close()
    svc = SkyTeamService(
        provider, store, zones, default_horizon_days=60, **svc_kwargs,
    )
    return svc, seen


def _entries_for_dates(*dates: str) -> dict:
    return {"data": [
        {
            "Route": {"OriginAirport": "CPH", "DestinationAirport": "BKK", "Source": "flyingblue"},
            "Date": d,
            "YAvailable": True, "YRemainingSeats": 3, "YAirlines": "SK", "YDirect": False,
            "JAvailable": True, "JRemainingSeats": 2, "JAirlines": "KL", "JDirect": True,
        }
        for d in dates
    ], "hasMore": False, "cursor": None}


def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


async def test_region_expands_to_catalog_airports(tmp_db, tmp_path, zones):
    svc, seen = _service(tmp_db, tmp_path, zones, [_entries_for_dates(_future(10))])
    result = await svc.search(origins=["CPH"], region="ASIA")
    # ASIA resolves via country names to BKK+NRT (BOS is NORTH_AMERICA, homes excluded).
    assert result.destinations == ["BKK", "NRT"]
    assert seen[0].url.params["destination_airport"] == "BKK,NRT"
    assert seen[0].url.params["origin_airport"] == "CPH"
    assert result.total == 2      # Y + J rows on the one date


async def test_unknown_region_raises_value_error(tmp_db, tmp_path, zones):
    svc, _ = _service(tmp_db, tmp_path, zones, [_entries_for_dates(_future(10))])
    with pytest.raises(ValueError, match="refresh the network catalog"):
        await svc.search(origins=["CPH"], region="ATLANTIS")


async def test_no_destinations_is_rejected_before_spending_budget(tmp_db, tmp_path, zones):
    # Live-verified: /search with no destination_airport silently returns nothing — fail
    # loudly instead of burning a budgeted call on a guaranteed-empty response.
    svc, seen = _service(tmp_db, tmp_path, zones, [_entries_for_dates(_future(10))])
    with pytest.raises(ValueError, match="pick a region or name destination"):
        await svc.search(origins=["CPH"])
    assert seen == []


async def test_filters_cabin_min_seats_sas_direct(tmp_db, tmp_path, zones):
    svc, _ = _service(tmp_db, tmp_path, zones, [_entries_for_dates(_future(5))])
    # cabin filter
    r = await svc.search(origins=["CPH"], destinations=["BKK"], cabin="AB")
    assert {row.cabin for row in r.rows} == {"AB"}
    # sas_only drops the KL business row
    r = await svc.search(origins=["CPH"], destinations=["BKK"], sas_only=True)
    assert {row.cabin for row in r.rows} == {"AG"}
    # min_seats=3 drops the 2-seat J row
    r = await svc.search(origins=["CPH"], destinations=["BKK"], min_seats=3)
    assert {row.cabin for row in r.rows} == {"AG"}
    # direct_only keeps only J (YDirect false)
    r = await svc.search(origins=["CPH"], destinations=["BKK"], direct_only=True)
    assert {row.cabin for row in r.rows} == {"AB"}


async def test_default_window_and_past_clamp(tmp_db, tmp_path, zones):
    svc, seen = _service(tmp_db, tmp_path, zones, [_entries_for_dates(_future(3))])
    result = await svc.search(origins=["CPH"], destinations=["BKK"], date_from="2020-01-01")
    today = date.today().isoformat()
    assert result.date_from == today                       # past start clamped
    assert result.date_to == _future(60)                   # default horizon
    assert seen[0].url.params["start_date"] == today


async def test_truncation_reports_total(tmp_db, tmp_path, zones):
    dates = [_future(i + 1) for i in range(6)]
    svc, _ = _service(tmp_db, tmp_path, zones, [_entries_for_dates(*dates)], max_rows=5)
    result = await svc.search(origins=["CPH"], destinations=["BKK"])
    assert result.total == 12          # 6 dates x 2 cabins
    assert result.truncated is True
    assert len(result.rows) == 5


async def test_search_spends_the_seats_aero_budget(tmp_db, tmp_path, zones):
    svc, _ = _service(tmp_db, tmp_path, zones, [_entries_for_dates(_future(2))])
    await svc.search(origins=["CPH"], destinations=["BKK"])
    assert Budget(tmp_db, 50, provider="seats_aero").used() == 1


async def test_budget_exceeded_blocks_before_http(tmp_db, tmp_path, zones):
    svc, seen = _service(tmp_db, tmp_path, zones, [_entries_for_dates(_future(2))],
                         daily_limit=0)
    with pytest.raises(BudgetExceeded, match="seats_aero"):
        await svc.search(origins=["CPH"], destinations=["BKK"])
    assert seen == []


# ---- provider-scoped budget + registry -----------------------------------------------------


def test_budget_provider_filter_separates_pools(tmp_db):
    all_budget = Budget(tmp_db, 10)
    all_budget.record(ProviderCall("route", "CPH", "BOS", "ok", 200, 100, 5))  # sas_direct
    sa = Budget(tmp_db, 10, provider="seats_aero")
    assert sa.used() == 0              # SAS calls don't eat the seats.aero cap
    assert all_budget.used() == 1      # provider=None still counts everything
    sa.record(ProviderCall("route", "CPH", "BKK", "ok", 200, 100, 5, provider="seats_aero"))
    assert sa.used() == 1
    assert all_budget.used() == 2


def _settings(**overrides):
    from app.config import Settings

    return Settings(_env_file=None, **overrides)


def test_build_skyteam_provider_needs_a_key(tmp_path):
    assert build_skyteam_provider(_settings(data_dir=tmp_path)) is None
    provider = build_skyteam_provider(
        _settings(data_dir=tmp_path, seats_aero_api_key="k")
    )
    assert isinstance(provider, SeatsAeroProvider)
    # Its budget is scoped so SAS traffic can't exhaust it.
    assert provider._budget.provider == "seats_aero"


# ---- fetch() refactor contract -------------------------------------------------------------


async def test_search_entries_paginates_and_returns_raw(tmp_db, sa_raw):
    data = json.loads(sa_raw)["data"]
    pages = [
        {"data": data[:3], "hasMore": True, "cursor": "abc"},
        {"data": data[3:], "hasMore": False, "cursor": None},
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[min(len(seen) - 1, 1)])

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://seats.aero/partnerapi",
    )
    provider = SeatsAeroProvider(
        "k", RateLimiter(0, 0), Budget(tmp_db, 50, provider="seats_aero"), client=client,
    )
    entries = await provider.search_entries(
        ["CPH", "OSL"], ["BKK", "NRT"], start_date="2026-10-01", end_date="2026-10-31",
    )
    assert len(entries) == len(data)                 # raw entries, unfiltered
    assert seen[0].url.params["origin_airport"] == "CPH,OSL"
    assert seen[0].url.params["destination_airport"] == "BKK,NRT"
    assert seen[1].url.params["cursor"] == "abc"
    assert Budget(tmp_db, 50, provider="seats_aero").used() == 2
