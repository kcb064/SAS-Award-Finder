"""Daily request budget + provider-call audit, backed by the `provider_calls` table.

Every SAS request (success or failure) is recorded. The budget is a hard daily cap so a bug or a
retry storm can't hammer SAS from the home IP. SQLite calls here are synchronous but sub-millisecond
for this single-user workload.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app import db


@dataclass(slots=True)
class ProviderCall:
    scope: str                 # 'network' | 'route'
    origin: str
    destination: str | None
    status: str                # 'ok' | 'failed'
    http_status: int | None
    byte_size: int
    duration_ms: int
    provider: str = "sas_direct"


class BudgetExceeded(RuntimeError):
    """Raised when a fetch would exceed the configured daily request budget."""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Budget:
    def __init__(self, db_path: Path, daily_limit: int) -> None:
        self.db_path = db_path
        self.daily_limit = daily_limit

    def used(self, day: str | None = None) -> int:
        day = day or _today()
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE call_date = ?", (day,)
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()

    def remaining(self, day: str | None = None) -> int:
        return max(0, self.daily_limit - self.used(day))

    def check(self) -> None:
        """Raise BudgetExceeded if no budget is left for today. Call before a fetch."""
        if self.remaining() <= 0:
            raise BudgetExceeded(
                f"daily SAS request budget of {self.daily_limit} reached for {_today()}"
            )

    def record(self, call: ProviderCall) -> None:
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO provider_calls
                   (call_date, provider, scope, origin, destination, status, http_status,
                    byte_size, duration_ms, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    _today(),
                    call.provider,
                    call.scope,
                    call.origin,
                    call.destination,
                    call.status,
                    call.http_status,
                    call.byte_size,
                    call.duration_ms,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        finally:
            conn.close()
