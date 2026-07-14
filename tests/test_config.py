"""Settings parsing from environment variables.

Regression: list-typed fields (home_airports, notify_urls) must accept the comma-separated
strings documented in .env.example. Without NoDecode, pydantic-settings JSON-decodes them at
the env-source layer and startup dies with "error parsing value for field".
"""
from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def _no_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Run each test from an empty cwd so a developer's real .env never leaks in."""
    monkeypatch.chdir(tmp_path)


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AF_HOME_AIRPORTS", raising=False)
    monkeypatch.delenv("AF_NOTIFY_URLS", raising=False)
    s = Settings()
    assert s.home_airports == ["CPH"]
    assert s.notify_urls == []


def test_single_airport_plain_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AF_HOME_AIRPORTS", "CPH")
    assert Settings().home_airports == ["CPH"]


def test_csv_airports_stripped_and_uppercased(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AF_HOME_AIRPORTS", "cph, osl,ARN")
    assert Settings().home_airports == ["CPH", "OSL", "ARN"]


def test_empty_notify_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AF_NOTIFY_URLS", "")
    assert Settings().notify_urls == []


def test_csv_notify_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AF_NOTIFY_URLS", "ntfy://awards, tgram://tok/chat")
    assert Settings().notify_urls == ["ntfy://awards", "tgram://tok/chat"]
