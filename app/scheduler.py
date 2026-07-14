"""Background scheduling (APScheduler, async).

Five jobs:
- network_refresh: periodically refresh the outbound-only NETWORK snapshot per home airport
  (keeps the destination catalog and the Explore overview current).
- watch_sweep: re-check every enabled watch (grouped by route — one scoped request per watched
  route), diff, alert. Jittered so the traffic pattern doesn't look like a cron.
- alert_delivery: retry undelivered alerts until they go through (the runner also delivers
  inline after each sweep; this job is the retry backstop).
- explore_sweep: nightly budgeted refresh of round-trip Explore leads. Centered on a quiet hour
  with ±2h jitter, and hard-capped per run, so it stays polite and watches keep budget priority.
- prune: daily cleanup of aged-out snapshots/observations and departed-date rows. Runs after
  the explore window so it never races the night's writes.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from app.providers.base import SCOPE_NETWORK, AwardProvider
from app.services.explore import ExploreSweeper
from app.services.notify import Notifier
from app.services.snapshots import SnapshotStore
from app.services.watches import WatchRunner

log = logging.getLogger("award_finder.scheduler")


async def refresh_network_snapshots(
    provider: AwardProvider, store: SnapshotStore, origins: list[str]
) -> None:
    for origin in origins:
        try:
            pf = await provider.fetch(SCOPE_NETWORK, origin)
            snapshot_id = store.persist(pf)
            log.info(
                "network refresh ok: origin=%s dests=%d bytes=%d snapshot_id=%s",
                origin, len(pf.feed.destinations), pf.byte_size, snapshot_id,
            )
        except Exception:  # noqa: BLE001 — a fetch failure must not kill the scheduler
            log.exception("network refresh FAILED for origin=%s", origin)


async def sweep_watches(runner: WatchRunner) -> None:
    try:
        await runner.run_all()
    except Exception:  # noqa: BLE001
        log.exception("watch sweep crashed")


async def deliver_alerts(notifier: Notifier) -> None:
    try:
        delivered = await notifier.deliver_pending()
        if delivered:
            log.info("alert retry delivered %d alert(s)", delivered)
    except Exception:  # noqa: BLE001
        log.exception("alert delivery crashed")


async def sweep_explore(sweeper: ExploreSweeper, origins: list[str]) -> None:
    try:
        summary = await sweeper.run_all(origins)
        log.info("explore sweep done: %s", summary)
    except Exception:  # noqa: BLE001
        log.exception("explore sweep crashed")


async def prune_data(store: SnapshotStore, retention_days: int) -> None:
    try:
        store.prune(retention_days)
    except Exception:  # noqa: BLE001
        log.exception("prune crashed")


def build_scheduler(
    settings: Settings,
    provider: AwardProvider,
    store: SnapshotStore,
    runner: WatchRunner,
    notifier: Notifier,
    explore_sweeper: ExploreSweeper,
) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(
        refresh_network_snapshots,
        trigger="interval",
        hours=settings.network_refresh_hours,
        args=[provider, store, settings.home_airports],
        id="network_refresh",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        sweep_watches,
        trigger="interval",
        minutes=settings.watch_refresh_minutes,
        jitter=120,
        args=[runner],
        id="watch_sweep",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        deliver_alerts,
        trigger="interval",
        minutes=settings.alert_retry_minutes,
        args=[notifier],
        id="alert_delivery",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        sweep_explore,
        trigger="cron",
        hour=settings.explore_sweep_hour_utc,
        minute=0,
        jitter=7200,
        args=[explore_sweeper, settings.home_airports],
        id="explore_sweep",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    sched.add_job(
        prune_data,
        trigger="cron",
        hour=(settings.explore_sweep_hour_utc + 3) % 24,
        minute=30,
        args=[store, settings.snapshot_retention_days],
        id="prune",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    return sched
