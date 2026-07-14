"""Shared pytest fixtures: fixture-file loading, a synthetic route feed, and the seed zone table."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import AwardFlight
from app.services.value import ZoneTable

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = Path(__file__).parent.parent


@pytest.fixture
def route_bos_raw() -> str:
    return (FIXTURES / "award_finder_availability_cph_bos.json").read_text(encoding="utf-8")


@pytest.fixture
def route_bos_json(route_bos_raw: str) -> list[dict]:
    return json.loads(route_bos_raw)


@pytest.fixture
def network_raw() -> str:
    return (FIXTURES / "network_cph_outbound.json").read_text(encoding="utf-8")


@pytest.fixture
def network_json(network_raw: str) -> list[dict]:
    return json.loads(network_raw)


@pytest.fixture
def zones() -> ZoneTable:
    return ZoneTable.load(PROJECT / "config" / "points_table.yaml")


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """A migrated (001+002) empty database in a temp dir."""
    from app.db import init_db

    p = tmp_path / "test.db"
    init_db(p)
    return p


def make_flight(
    *, direction: str, date: str, cabin: str, seats: int,
    origin: str = "CPH", destination: str = "BOS", seats_total: int | None = None,
    sas: bool = True,
) -> AwardFlight:
    """Terse constructor for hand-built AwardFlight rows in pairing tests."""
    return AwardFlight(
        origin=origin, destination=destination, direction=direction, flight_date=date,
        cabin=cabin, seats=seats, seats_total=seats_total if seats_total is not None else seats,
        is_sas_operated=sas,
    )
