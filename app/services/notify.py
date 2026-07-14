"""Alert persistence + Apprise push delivery.

Alerts are written to the `alerts` table first (dedup enforced by the unique index on dedup_key),
then delivered asynchronously: `deliver_pending` pushes every undelivered alert through Apprise
and stamps `delivered_at` on success. Failures stay pending and are retried by the scheduler's
delivery job until they go through. With no notification URLs configured, alerts are log-only:
they're stamped delivered immediately so the retry queue never grows.

Apprise handles the transport zoo (Telegram/Discord/ntfy/...) from config URLs; its `notify()` is
synchronous, so delivery runs in a worker thread to keep the event loop free.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import apprise

from app import db

log = logging.getLogger("award_finder.notify")

ALERT_TYPES = ("opened", "price_drop", "closed", "voucher_pair", "ops")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_utc() -> str:
    """UTC day bucket used in dedup keys — the same alert may re-fire on a later day."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True, slots=True)
class AlertDraft:
    type: str
    dedup_key: str
    title: str
    body: str
    watch_id: int | None = None
    outbound_date: str | None = None
    inbound_date: str | None = None
    cabin: str | None = None


def ops_alert(category: str, message: str) -> AlertDraft:
    """An operational alert (provider unhealthy, parse failure, budget blocked), deduped per day."""
    return AlertDraft(
        type="ops",
        dedup_key=f"ops|{category}|{today_utc()}",
        title=f"⚠️ Award finder: {category}",
        body=message,
    )


class AlertStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def insert(self, draft: AlertDraft) -> int | None:
        """Insert an alert; returns its id, or None when the dedup key already exists."""
        conn = db.connect(self.db_path)
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO alerts
                   (watch_id, type, dedup_key, title, body, outbound_date, inbound_date, cabin,
                    created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    draft.watch_id, draft.type, draft.dedup_key, draft.title, draft.body,
                    draft.outbound_date, draft.inbound_date, draft.cabin, _now(),
                ),
            )
            return cur.lastrowid if cur.rowcount else None
        finally:
            conn.close()

    def pending(self, limit: int = 50) -> list[dict]:
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                """SELECT * FROM alerts WHERE delivered_at IS NULL
                   ORDER BY created_at ASC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def recent(self, limit: int = 100) -> list[dict]:
        """Alert log for the UI, newest first, with the watch label joined in."""
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                """SELECT a.*, w.label AS watch_label, w.origin AS watch_origin,
                          w.destination AS watch_destination
                   FROM alerts a LEFT JOIN watches w ON w.id = a.watch_id
                   ORDER BY a.created_at DESC, a.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def record_attempt(self, alert_id: int, delivered: bool) -> None:
        conn = db.connect(self.db_path)
        try:
            if delivered:
                conn.execute(
                    """UPDATE alerts SET delivered_at = ?,
                       delivery_attempts = delivery_attempts + 1 WHERE id = ?""",
                    (_now(), alert_id),
                )
            else:
                conn.execute(
                    "UPDATE alerts SET delivery_attempts = delivery_attempts + 1 WHERE id = ?",
                    (alert_id,),
                )
        finally:
            conn.close()

    def mark_delivered_without_send(self, alert_id: int) -> None:
        """Log-only mode (no notify URLs): the Alerts page IS the delivery."""
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE alerts SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                (_now(), alert_id),
            )
        finally:
            conn.close()


class Notifier:
    def __init__(self, store: AlertStore, notify_urls: list[str]) -> None:
        self._store = store
        self._urls = [u for u in notify_urls if u.strip()]
        self._apprise: apprise.Apprise | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._urls)

    def _client(self) -> apprise.Apprise:
        if self._apprise is None:
            client = apprise.Apprise()
            for url in self._urls:
                if not client.add(url):
                    log.error("invalid Apprise URL (check AF_NOTIFY_URLS): %s...", url[:20])
            self._apprise = client
        return self._apprise

    def _send(self, title: str, body: str) -> bool:
        """Blocking Apprise send; overridden in tests. Truthy = at least one target accepted it."""
        return bool(self._client().notify(title=title, body=body))

    async def deliver_pending(self) -> int:
        """Push all undelivered alerts; returns how many were delivered this pass."""
        pending = self._store.pending()
        if not pending:
            return 0
        if not self.enabled:
            for alert in pending:
                self._store.mark_delivered_without_send(alert["id"])
            log.info("no notify URLs configured — %d alert(s) logged only", len(pending))
            return len(pending)

        delivered = 0
        for alert in pending:
            try:
                ok = await asyncio.to_thread(self._send, alert["title"], alert["body"] or "")
            except Exception:  # noqa: BLE001 — a transport blowup must not kill the delivery loop
                log.exception("alert delivery raised (id=%s)", alert["id"])
                ok = False
            self._store.record_attempt(alert["id"], ok)
            if ok:
                delivered += 1
            else:
                log.warning(
                    "alert delivery failed (id=%s, attempt %s) — will retry",
                    alert["id"], alert["delivery_attempts"] + 1,
                )
        return delivered
