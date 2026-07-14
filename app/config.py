"""Application settings, loaded from environment / .env via pydantic-settings.

All fields are prefixed `AF_` in the environment (see .env.example). Values are parsed once at
import of `get_settings()` and cached, so the rest of the app reads a single immutable object.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    # NoDecode: env values are comma-separated ("CPH,OSL"), not JSON — _split_csv parses them.
    home_airports: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["CPH"])
    data_dir: Path = Path("./data")
    config_dir: Path = Path("./config")
    port: int = 8617

    # Scheduler cadence
    network_refresh_hours: float = 6.0

    # Provider (Phase 4): 'sas_direct' scrapes flysas.com; 'seats_aero' is the paid API fallback.
    provider: str = "sas_direct"
    seats_aero_api_key: str = ""
    seats_aero_source: str = "eurobonus"

    # Fetching
    headless: bool = True
    browser_channel: str = "chrome"
    fetch_min_interval_s: float = 4.0
    fetch_max_interval_s: float = 8.0
    daily_request_budget: int = 500
    snapshot_ttl_s: int = 900
    snapshot_retention_days: int = 30    # raw snapshots + observation history kept this long

    # Voucher / value
    voucher_count: int = 1

    # Watches / alerts (Phase 2)
    watch_refresh_minutes: int = 60      # how often the sweep re-checks every enabled watch
    alert_retry_minutes: int = 5         # undelivered alerts are retried on this cadence
    ops_failure_threshold: int = 3       # consecutive sweep failures before an ops alert

    # Explore (Phase 3)
    explore_sweep_budget: int = 25       # max route fetches per nightly sweep, per origin
    explore_sweep_hour_utc: int = 4      # nightly sweep center; ±2h jitter spreads it over 02-06
    explore_min_stay_days: int = 3       # default stay window leads are paired within
    explore_max_stay_days: int = 14

    # Notifications
    notify_urls: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Auth
    basic_auth_user: str = ""
    basic_auth_pass: str = ""

    # Scheduler
    scheduler_enabled: bool = True

    @field_validator("home_airports", "notify_urls", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept a comma-separated string from env and turn it into a clean list."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("home_airports")
    @classmethod
    def _upper_airports(cls, v: list[str]) -> list[str]:
        return [code.upper() for code in v]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "award_finder.db"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def points_table_path(self) -> Path:
        return self.config_dir / "points_table.yaml"

    @property
    def default_home(self) -> str:
        return self.home_airports[0] if self.home_airports else "CPH"

    @property
    def basic_auth_enabled(self) -> bool:
        return bool(self.basic_auth_user and self.basic_auth_pass)

    def ensure_dirs(self) -> None:
        """Create the runtime directories if they do not exist. Called once at startup."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
