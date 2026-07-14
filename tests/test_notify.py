"""Alert store dedup + Apprise delivery/retry behavior (with a fake transport)."""
from __future__ import annotations

from pathlib import Path

from app.services.notify import AlertDraft, AlertStore, Notifier, ops_alert, today_utc


def draft(key: str = "k1", **over) -> AlertDraft:
    base = dict(
        type="opened", dedup_key=key, title="t", body="b",
        watch_id=None, outbound_date=None, inbound_date=None, cabin=None,
    )
    base.update(over)
    return AlertDraft(**base)


def test_dedup_key_blocks_second_insert(tmp_db: Path):
    store = AlertStore(tmp_db)
    assert store.insert(draft()) is not None
    assert store.insert(draft()) is None                 # same key -> suppressed
    assert store.insert(draft(key="k2")) is not None     # different key -> new alert
    assert len(store.recent()) == 2


def test_ops_alert_dedups_per_category_and_day():
    a = ops_alert("provider unhealthy", "boom")
    assert a.type == "ops"
    assert a.dedup_key == f"ops|provider unhealthy|{today_utc()}"


class FlakySender(Notifier):
    """Fails the first `fail_times` sends, then succeeds."""

    def __init__(self, store: AlertStore, fail_times: int = 0) -> None:
        super().__init__(store, ["json://fake"])
        self.fail_times = fail_times
        self.calls = 0

    def _send(self, title: str, body: str) -> bool:
        self.calls += 1
        if self.calls <= self.fail_times:
            return False
        return True


async def test_delivery_success_stamps_delivered(tmp_db: Path):
    store = AlertStore(tmp_db)
    store.insert(draft())
    n = FlakySender(store)
    assert await n.deliver_pending() == 1
    (alert,) = store.recent()
    assert alert["delivered_at"] is not None and alert["delivery_attempts"] == 1
    assert store.pending() == []


async def test_failed_delivery_stays_pending_and_retries(tmp_db: Path):
    store = AlertStore(tmp_db)
    store.insert(draft())
    n = FlakySender(store, fail_times=2)

    assert await n.deliver_pending() == 0   # attempt 1 fails
    assert await n.deliver_pending() == 0   # attempt 2 fails
    (alert,) = store.recent()
    assert alert["delivered_at"] is None and alert["delivery_attempts"] == 2

    assert await n.deliver_pending() == 1   # attempt 3 lands
    (alert,) = store.recent()
    assert alert["delivered_at"] is not None and alert["delivery_attempts"] == 3


async def test_log_only_mode_marks_delivered_without_sending(tmp_db: Path):
    store = AlertStore(tmp_db)
    store.insert(draft())
    n = Notifier(store, notify_urls=[])     # no URLs configured
    assert not n.enabled
    assert await n.deliver_pending() == 1
    assert store.pending() == []
