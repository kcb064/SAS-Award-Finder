"""SQLite access + a tiny migration runner.

One connection factory (`connect`), WAL mode, and a `run_migrations` step that applies numbered
`migrations/NNN_*.sql` files whose number is greater than the DB's current `PRAGMA user_version`.
No ORM: this app's data is simple rows and a few hot upserts, so raw SQL keeps it legible.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection tuned for a single-writer homelab app.

    Row factory returns `sqlite3.Row` so callers can use column names. WAL + a generous
    busy_timeout keep the background scheduler and the web requests from tripping over each other.
    """
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _discover_migrations() -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _MIGRATION_RE.match(path.name)
        if m:
            found.append((int(m.group(1)), path))
    found.sort(key=lambda t: t[0])
    return found


def run_migrations(conn: sqlite3.Connection) -> int:
    """Apply any migrations newer than the DB's `user_version`. Returns the resulting version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, path in _discover_migrations():
        if version <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        # executescript() implicitly commits any pending transaction, so transaction control must
        # live inside the script itself. Bumping user_version in the same script keeps it atomic:
        # a failure rolls back both the DDL and the version bump.
        script = f"BEGIN;\n{sql}\nPRAGMA user_version={version};\nCOMMIT;"
        try:
            conn.executescript(script)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        current = version
    return current


def init_db(db_path: Path) -> None:
    """Ensure the DB file exists and is migrated to the latest schema version."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        run_migrations(conn)
    finally:
        conn.close()
