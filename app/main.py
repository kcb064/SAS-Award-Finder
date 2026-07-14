"""FastAPI application: lifespan wiring, optional basic-auth gate, routes + static.

The lifespan builds the whole object graph once (store, provider, fetcher, search service, scheduler)
and hangs it off `app.state.services`. The browser fetcher is created but NOT started here — it starts
lazily on the first live fetch, so booting the app (and running Search against cached data) is cheap.
"""
from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import Settings, get_settings
from app.db import init_db
from app.fetch.engine import BrowserFetcher
from app.providers.base import AwardProvider
from app.providers.registry import build_fetcher, build_provider
from app.scheduler import build_scheduler
from app.services.explore import ExploreStore, ExploreSweeper
from app.services.notify import AlertStore, Notifier
from app.services.search import SearchService
from app.services.snapshots import SnapshotStore
from app.services.value import CashFareStore, TripValueService, ZoneTable
from app.services.watches import WatchRunner, WatchStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("award_finder")


@dataclass
class Services:
    settings: Settings
    store: SnapshotStore
    provider: AwardProvider
    fetcher: BrowserFetcher
    search: SearchService
    zones: ZoneTable
    cash_fares: CashFareStore
    values: TripValueService
    watches: WatchStore
    alerts: AlertStore
    notifier: Notifier
    watch_runner: WatchRunner
    explore: ExploreStore
    explore_sweeper: ExploreSweeper
    scheduler: object | None = None


def build_services(settings: Settings) -> Services:
    settings.ensure_dirs()
    init_db(settings.db_path)

    store = SnapshotStore(settings.db_path, settings.snapshots_dir)
    store.seed_home_airports(settings.home_airports)
    zones = ZoneTable.load(settings.points_table_path)
    fetcher = build_fetcher(settings)
    provider = build_provider(settings, fetcher)
    cash_fares = CashFareStore(settings.db_path)
    values = TripValueService(cash_fares, zones)
    search = SearchService(
        provider, store, zones, snapshot_ttl_s=settings.snapshot_ttl_s, values=values,
    )
    watches = WatchStore(settings.db_path)
    alerts = AlertStore(settings.db_path)
    notifier = Notifier(alerts, settings.notify_urls)
    watch_runner = WatchRunner(
        provider, store, watches, alerts, notifier, zones,
        snapshot_ttl_s=settings.snapshot_ttl_s,
        ops_failure_threshold=settings.ops_failure_threshold,
        values=values,
    )
    explore = ExploreStore(settings.db_path, store, zones)
    explore_sweeper = ExploreSweeper(
        provider, store, explore, zones, alerts,
        snapshot_ttl_s=settings.snapshot_ttl_s,
        per_run_budget=settings.explore_sweep_budget,
        min_stay_days=settings.explore_min_stay_days,
        max_stay_days=settings.explore_max_stay_days,
    )
    return Services(
        settings=settings, store=store, provider=provider, fetcher=fetcher,
        search=search, zones=zones, cash_fares=cash_fares, values=values,
        watches=watches, alerts=alerts, notifier=notifier, watch_runner=watch_runner,
        explore=explore, explore_sweeper=explore_sweeper,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    services = build_services(settings)
    app.state.services = services

    if settings.scheduler_enabled:
        scheduler = build_scheduler(
            settings, services.provider, services.store,
            services.watch_runner, services.notifier, services.explore_sweeper,
        )
        scheduler.start()
        services.scheduler = scheduler
        log.info(
            "scheduler started: network refresh %.1fh, watch sweep %dmin, alert retry %dmin, "
            "explore sweep ~%02d:00 UTC (budget %d/origin)",
            settings.network_refresh_hours, settings.watch_refresh_minutes,
            settings.alert_retry_minutes, settings.explore_sweep_hour_utc,
            settings.explore_sweep_budget,
        )

    log.info("award finder ready on port %d (homes=%s)", settings.port, settings.home_airports)
    try:
        yield
    finally:
        if services.scheduler is not None:
            services.scheduler.shutdown(wait=False)
        if services.fetcher.started:
            await services.fetcher.aclose()
        if hasattr(services.provider, "aclose"):    # seats.aero holds an httpx client
            await services.provider.aclose()
        log.info("award finder shut down")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Optional whole-site basic-auth gate (used when not fronted by Cloudflare Access)."""

    def __init__(self, app, user: str, password: str) -> None:
        super().__init__(app)
        self._user = user
        self._password = password

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                user, _, pwd = decoded.partition(":")
                if secrets.compare_digest(user, self._user) and secrets.compare_digest(
                    pwd, self._password
                ):
                    return await call_next(request)
            except Exception:  # noqa: BLE001
                pass
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Award Finder"'},
            content="Authentication required",
        )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="EuroBonus Award Finder", version="0.1.0", lifespan=lifespan)

    if settings.basic_auth_enabled:
        app.add_middleware(
            BasicAuthMiddleware, user=settings.basic_auth_user, password=settings.basic_auth_pass
        )

    from app.web.routes import router  # imported here to avoid a circular import at module load

    app.include_router(router)
    static_dir = Path(__file__).parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return app


app = create_app()
