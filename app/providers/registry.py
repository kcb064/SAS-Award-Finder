"""Provider wiring: AF_PROVIDER selects SAS direct (default) or the seats.aero fallback. Both
sit behind the same `AwardProvider` protocol and share the rate-limit + daily-budget layer, so
everything above (search, watches, explore) is provider-blind.
"""
from __future__ import annotations

from app.config import Settings
from app.fetch.budget import Budget
from app.fetch.engine import BrowserFetcher
from app.fetch.ratelimit import RateLimiter
from app.providers.base import AwardProvider
from app.providers.sas_direct.provider import SASDirectProvider
from app.providers.seats_aero.provider import SeatsAeroProvider


def build_provider(settings: Settings, fetcher: BrowserFetcher) -> AwardProvider:
    """Construct the configured provider with its rate limiter and budget."""
    rate = RateLimiter(settings.fetch_min_interval_s, settings.fetch_max_interval_s)
    budget = Budget(settings.db_path, settings.daily_request_budget)
    if settings.provider == "seats_aero":
        if not settings.seats_aero_api_key:
            raise ValueError(
                "AF_PROVIDER=seats_aero needs AF_SEATS_AERO_API_KEY (seats.aero Pro subscription)"
            )
        return SeatsAeroProvider(
            settings.seats_aero_api_key, rate, budget, source=settings.seats_aero_source,
        )
    if settings.provider != "sas_direct":
        raise ValueError(
            f"unknown AF_PROVIDER {settings.provider!r} — expected 'sas_direct' or 'seats_aero'"
        )
    return SASDirectProvider(fetcher, rate, budget)


def build_fetcher(settings: Settings) -> BrowserFetcher:
    return BrowserFetcher(
        profile_dir=settings.browser_profile_dir,
        headless=settings.headless,
        channel=settings.browser_channel,
    )
