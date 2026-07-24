"""/skyteam route: setup notice, rendered rows + badges, NL wiring, and the voucher filter
contract (voucher intent forces sas_only + min_seats>=2). Services are faked — no live app."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.providers.seats_aero.parser import SkyTeamRow
from app.services.nl_search import NLParams, NLParseError
from app.services.skyteam import SkyTeamResult, SkyTeamTrip
from app.web.routes import router


def _row(**overrides) -> SkyTeamRow:
    base = dict(
        date="2026-10-05", origin="CPH", destination="BKK", cabin="AB",
        airlines=("KL",), seats=2, sas_operated=False, direct=True,
        mileage_cost=80000, total_taxes=None, taxes_currency=None, source="flyingblue",
    )
    base.update(overrides)
    return SkyTeamRow(**base)


def _result(rows: list[SkyTeamRow]) -> SkyTeamResult:
    return SkyTeamResult(
        rows=rows, total=len(rows), truncated=False, origins=["CPH"],
        destinations=["BKK"], region=None, date_from="2026-10-01", date_to="2026-10-31",
    )


def _trip(**overrides) -> SkyTeamTrip:
    base = dict(
        out=_row(airlines=("SK",), sas_operated=True),
        ret=_row(date="2026-10-12", origin="BKK", destination="CPH",
                 airlines=("SK",), sas_operated=True),
        stay_days=7, mileage_total=160000, taxes_total=962.0, taxes_currency="USD",
        direct=True, voucher_usable=True, sources=("flyingblue",),
    )
    base.update(overrides)
    return SkyTeamTrip(**base)


def _rt_result(trips: list[SkyTeamTrip]) -> SkyTeamResult:
    return SkyTeamResult(
        rows=[], total=len(trips), truncated=False, origins=["CPH"],
        destinations=["BKK"], region=None, date_from="2026-10-01", date_to="2026-10-31",
        trip_type="RT", trips=trips,
    )


class FakeSkyTeam:
    def __init__(self, rows: list[SkyTeamRow] | None = None,
                 result: SkyTeamResult | None = None):
        self.result = result if result is not None else _result(rows or [])
        self.calls: list[dict] = []

    def region_names(self):
        return ["ASIA", "EUROPE"]

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeNL:
    def __init__(self, params: NLParams | None = None, error: Exception | None = None):
        self.params = params
        self.error = error

    async def parse(self, query: str) -> NLParams:
        if self.error:
            raise self.error
        return self.params


def _client(skyteam=None, nl=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.services = SimpleNamespace(
        settings=SimpleNamespace(home_airports=["CPH"]),
        store=SimpleNamespace(list_destinations=lambda: []),
        skyteam=skyteam,
        nl=nl,
    )
    return TestClient(app)


def _nl_params(**overrides) -> NLParams:
    base = dict(
        origins=["CPH"], destinations=[], region="ASIA",
        date_from="2026-10-01", date_to="2026-10-31", cabin=None,
        min_seats=None, voucher_intent=True, trip_type="RT",
        summary="Flights CPH → Asia in October, 2-for-1 voucher intent.",
    )
    base.update(overrides)
    return NLParams(**base)


def test_no_key_shows_setup_notice():
    resp = _client(skyteam=None).get("/skyteam")
    assert resp.status_code == 200
    assert "AF_SEATS_AERO_API_KEY" in resp.text
    assert "Search SkyTeam space" not in resp.text


def test_rows_render_with_badges():
    sky = FakeSkyTeam([
        _row(),                                                  # partner, direct
        _row(cabin="AG", airlines=("SK",), sas_operated=True),   # voucher-usable (2 seats)
        _row(cabin="AP", airlines=("SK",), sas_operated=True, seats=0),  # unknown count -> 1+
    ])
    resp = _client(skyteam=sky).get("/skyteam", params={"destination": "BKK"})
    assert resp.status_code == 200
    assert "Partner" in resp.text
    assert "voucher-usable leg" in resp.text
    assert "1+" in resp.text
    assert "80,000" in resp.text     # the custom |int filter adds thousands separators
    # One-way honesty note is always under the table.
    assert "both legs" in resp.text


def test_voucher_checkbox_forces_sas_only_and_two_seats():
    sky = FakeSkyTeam([_row(airlines=("SK",), sas_operated=True)])
    _client(skyteam=sky).get(
        "/skyteam", params={"destination": "BKK", "voucher": 1, "min_seats": 1},
    )
    call = sky.calls[0]
    assert call["sas_only"] is True
    assert call["min_seats"] == 2


def test_rt_is_the_default_and_stay_params_flow_to_service():
    sky = FakeSkyTeam(result=_rt_result([_trip()]))
    _client(skyteam=sky).get(
        "/skyteam",
        params={"destination": "BKK", "min_stay_days": 5, "max_stay_days": 9, "submitted": 1},
    )
    call = sky.calls[0]
    assert call["trip_type"] == "RT"
    assert call["min_stay_days"] == 5
    assert call["max_stay_days"] == 9
    # The form was submitted without the collapse checkbox -> it was unchecked.
    assert call["collapse"] is False


def test_collapse_defaults_on_when_arriving_without_the_form():
    sky = FakeSkyTeam(result=_rt_result([_trip()]))
    _client(skyteam=sky).get("/skyteam", params={"destination": "BKK"})
    assert sky.calls[0]["collapse"] is True


def test_round_trips_render_with_stay_and_totals():
    sky = FakeSkyTeam(result=_rt_result([_trip()]))
    resp = _client(skyteam=sky).get("/skyteam", params={"destination": "BKK"})
    assert resp.status_code == 200
    assert "round-trip option" in resp.text
    assert "CPH ⇄ BKK" in resp.text
    assert "2026-10-05" in resp.text and "2026-10-12" in resp.text
    assert "7d" in resp.text
    assert "160,000" in resp.text
    assert "voucher-usable" in resp.text


def test_no_paired_round_trips_suggests_one_way_rows():
    sky = FakeSkyTeam(result=_rt_result([]))
    resp = _client(skyteam=sky).get("/skyteam", params={"destination": "BKK"})
    assert "No round-trips paired" in resp.text
    assert "One-way rows" in resp.text


def test_one_way_rows_keep_the_flat_table():
    sky = FakeSkyTeam([_row()])
    resp = _client(skyteam=sky).get(
        "/skyteam", params={"destination": "BKK", "trip_type": "OW", "submitted": 1},
    )
    assert sky.calls[0]["trip_type"] == "OW"
    assert "availability row" in resp.text


def test_nl_disabled_gives_friendly_error():
    sky = FakeSkyTeam([])
    resp = _client(skyteam=sky, nl=None).get("/skyteam", params={"q": "flights to asia"})
    assert "AF_ANTHROPIC_API_KEY" in resp.text
    assert sky.calls == []      # no search ran


def test_nl_success_populates_form_and_searches():
    sky = FakeSkyTeam([_row(airlines=("SK",), sas_operated=True)])
    nl = FakeNL(_nl_params())
    resp = _client(skyteam=sky, nl=nl).get("/skyteam", params={"q": "asia in october"})
    assert "Interpreted as:" in resp.text
    assert "2-for-1 voucher intent" in resp.text
    call = sky.calls[0]
    # Voucher intent flows into the deterministic filter, not just the badge.
    assert call["sas_only"] is True
    assert call["min_seats"] == 2
    assert call["region"] == "ASIA"
    assert call["date_from"] == "2026-10-01"
    assert call["trip_type"] == "RT"


def test_nl_parse_error_keeps_form_usable():
    sky = FakeSkyTeam([])
    nl = FakeNL(error=NLParseError("Couldn't interpret the query — use the form below."))
    resp = _client(skyteam=sky, nl=nl).get("/skyteam", params={"q": "???"})
    assert "use the form below" in resp.text
    assert "Search SkyTeam space" in resp.text     # structured form still rendered
    assert sky.calls == []
