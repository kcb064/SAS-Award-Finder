"""Snapshot persistence + read helpers.

Persists a `ProviderFetch` as: a raw JSON file on the data volume, one `availability_snapshots` row,
upserted `airports` catalog rows, append-only `award_flights` observations, and an updated
`award_current` (the diffing anchor for Phase 2). Also answers the read questions Search needs:
"is there a fresh snapshot for this route?" and "give me its flights." — and, since Phase 4,
prunes old snapshots so the append-only history doesn't grow forever.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app import db
from app.models import AwardFlight, DestinationInfo
from app.providers.base import ProviderFetch

log = logging.getLogger("award_finder.snapshots")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class SnapshotStore:
    def __init__(self, db_path: Path, snapshots_dir: Path) -> None:
        self.db_path = db_path
        self.snapshots_dir = snapshots_dir

    # ---- seeding / catalog -----------------------------------------------------------

    def seed_home_airports(self, codes: list[str]) -> None:
        conn = db.connect(self.db_path)
        try:
            now = _now()
            for code in codes:
                conn.execute(
                    """INSERT INTO airports (code, is_home, updated_at) VALUES (?, 1, ?)
                       ON CONFLICT(code) DO UPDATE SET is_home=1, updated_at=excluded.updated_at""",
                    (code.upper(), now),
                )
        finally:
            conn.close()

    def _upsert_airports(self, conn, destinations: list[DestinationInfo]) -> None:
        # COALESCE keeps existing metadata when a sparser catalog (seats.aero is codes-only)
        # upserts the same airport — a fallback fetch must not blank out SAS-provided names.
        now = _now()
        for d in destinations:
            conn.execute(
                """INSERT INTO airports
                   (code, city_name, country_name, city_code, lat, lng, flight_classes, image, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(code) DO UPDATE SET
                     city_name=COALESCE(excluded.city_name, city_name),
                     country_name=COALESCE(excluded.country_name, country_name),
                     city_code=COALESCE(excluded.city_code, city_code),
                     lat=COALESCE(excluded.lat, lat), lng=COALESCE(excluded.lng, lng),
                     flight_classes=COALESCE(excluded.flight_classes, flight_classes),
                     image=COALESCE(excluded.image, image),
                     updated_at=excluded.updated_at""",
                (
                    d.code, d.city_name, d.country_name, d.city_code, d.lat, d.lng,
                    json.dumps(list(d.flight_classes)) if d.flight_classes else None,
                    d.image, now,
                ),
            )

    def country_for(self, code: str) -> str | None:
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT country_name FROM airports WHERE code = ?", (code.upper(),)
            ).fetchone()
            return row["country_name"] if row else None
        finally:
            conn.close()

    def list_destinations(self) -> list[dict]:
        """Catalog rows (excluding home airports), for the Search destination picker."""
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                """SELECT code, city_name, country_name, flight_classes
                   FROM airports WHERE is_home = 0 ORDER BY city_name IS NULL, city_name, code"""
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def home_airports(self) -> list[str]:
        conn = db.connect(self.db_path)
        try:
            return [r["code"] for r in conn.execute(
                "SELECT code FROM airports WHERE is_home = 1 ORDER BY code"
            ).fetchall()]
        finally:
            conn.close()

    # ---- persistence -----------------------------------------------------------------

    def persist(self, pf: ProviderFetch) -> int:
        """Persist one fetch; return the new snapshot_id."""
        now = _now()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest_part = pf.destination or "ALL"
        raw_name = f"{pf.origin}_{pf.scope}_{dest_part}_{ts}.json"
        raw_path = self.snapshots_dir / raw_name
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(pf.raw_text, encoding="utf-8")

        conn = db.connect(self.db_path)
        try:
            conn.execute("BEGIN")
            cur = conn.execute(
                """INSERT INTO availability_snapshots
                   (origin, scope, destination, fetched_at, status, dest_count, byte_size, raw_path)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    pf.origin, pf.scope, pf.destination, now, pf.status,
                    len(pf.feed.destinations), pf.byte_size, str(raw_path),
                ),
            )
            snapshot_id = cur.lastrowid
            self._upsert_airports(conn, pf.feed.destinations)

            flight_rows = [
                (
                    f.key, snapshot_id, f.origin, f.destination, f.direction, f.flight_date,
                    f.cabin, f.seats, f.seats_total, int(f.is_sas_operated), now,
                )
                for f in pf.feed.flights
            ]
            conn.executemany(
                """INSERT INTO award_flights
                   (flight_key, snapshot_id, origin, destination, direction, flight_date, cabin,
                    seats, seats_total, is_sas_operated, observed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                flight_rows,
            )
            for f in pf.feed.flights:
                conn.execute(
                    """INSERT INTO award_current
                       (flight_key, origin, destination, direction, flight_date, cabin, seats,
                        seats_total, is_sas_operated, first_seen_at, last_seen_at, last_snapshot_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(flight_key) DO UPDATE SET
                         seats=excluded.seats, seats_total=excluded.seats_total,
                         is_sas_operated=excluded.is_sas_operated,
                         last_seen_at=excluded.last_seen_at, last_snapshot_id=excluded.last_snapshot_id""",
                    (
                        f.key, f.origin, f.destination, f.direction, f.flight_date, f.cabin,
                        f.seats, f.seats_total, int(f.is_sas_operated), now, now, snapshot_id,
                    ),
                )
            conn.execute("COMMIT")
            return snapshot_id
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ---- diffing anchor (Phase 2) ------------------------------------------------------

    def current_route_state(self, origin: str, destination: str) -> list[dict]:
        """The previously known `award_current` rows for one route (both directions) — the
        state a fresh route sweep is diffed against. Must be read BEFORE persist() updates it."""
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                """SELECT flight_key, origin, destination, direction, flight_date, cabin, seats,
                          seats_total, is_sas_operated
                   FROM award_current WHERE origin = ? AND destination = ?""",
                (origin.upper(), destination.upper()),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def prune_current_route(self, origin: str, destination: str, keep_keys: set[str]) -> int:
        """Drop `award_current` rows for a route that a fully-ok sweep no longer reported —
        those awards are gone. Never called for partial/failed sweeps. Returns rows removed."""
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT flight_key FROM award_current WHERE origin = ? AND destination = ?",
                (origin.upper(), destination.upper()),
            ).fetchall()
            stale = [r["flight_key"] for r in rows if r["flight_key"] not in keep_keys]
            for key in stale:
                conn.execute("DELETE FROM award_current WHERE flight_key = ?", (key,))
            return len(stale)
        finally:
            conn.close()

    # ---- reads for Search ------------------------------------------------------------

    def latest_snapshot(self, origin: str, scope: str, destination: str | None = None) -> dict | None:
        conn = db.connect(self.db_path)
        try:
            if destination is None:
                row = conn.execute(
                    """SELECT * FROM availability_snapshots
                       WHERE origin=? AND scope=? AND destination IS NULL AND status='ok'
                       ORDER BY fetched_at DESC LIMIT 1""",
                    (origin.upper(), scope),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM availability_snapshots
                       WHERE origin=? AND scope=? AND destination=? AND status='ok'
                       ORDER BY fetched_at DESC LIMIT 1""",
                    (origin.upper(), scope, destination.upper()),
                ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def is_fresh(self, snapshot: dict | None, ttl_s: int) -> bool:
        if not snapshot:
            return False
        age = (datetime.now(timezone.utc) - _parse_iso(snapshot["fetched_at"])).total_seconds()
        return age <= ttl_s

    def flights_by_snapshot(self, snapshot_id: int) -> list[AwardFlight]:
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                """SELECT origin, destination, direction, flight_date, cabin, seats, seats_total,
                          is_sas_operated
                   FROM award_flights WHERE snapshot_id = ?""",
                (snapshot_id,),
            ).fetchall()
            return [
                AwardFlight(
                    origin=r["origin"], destination=r["destination"], direction=r["direction"],
                    flight_date=r["flight_date"], cabin=r["cabin"], seats=r["seats"],
                    seats_total=r["seats_total"], is_sas_operated=bool(r["is_sas_operated"]),
                )
                for r in rows
            ]
        finally:
            conn.close()

    # ---- pruning (Phase 4) -------------------------------------------------------------

    def prune(self, retention_days: int, *, today: str | None = None) -> dict:
        """Trim history the app can never act on again. Returns per-table removal counts.

        - Snapshots older than the retention window go (row + observation rows + raw file),
          EXCEPT the newest snapshot per (origin, scope, destination) — Search's cache check,
          the Explore overview, and staleness badges always keep something to read, no matter
          how long the app was down.
        - `award_current` rows whose flight date has passed are dropped: they can't be booked
          and would otherwise read as a "closed" diff the day the date rolls over.
        - Explore leads whose outbound date has passed are dropped the same way (normally
          replaced on refresh; this catches destinations never refreshed again).
        - `sweep_runs` bookkeeping ages out with the same retention window.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        today = today or date.today().isoformat()
        conn = db.connect(self.db_path)
        try:
            conn.execute("BEGIN")
            doomed = conn.execute(
                """SELECT id, raw_path FROM availability_snapshots
                   WHERE fetched_at < ? AND id NOT IN (
                     SELECT MAX(id) FROM availability_snapshots
                     GROUP BY origin, scope, COALESCE(destination, ''))""",
                (cutoff,),
            ).fetchall()
            ids = [r["id"] for r in doomed]
            flights = 0
            for sid in ids:
                flights += conn.execute(
                    "DELETE FROM award_flights WHERE snapshot_id = ?", (sid,)
                ).rowcount
                conn.execute("DELETE FROM availability_snapshots WHERE id = ?", (sid,))
            departed = conn.execute(
                "DELETE FROM award_current WHERE flight_date < ?", (today,)
            ).rowcount
            leads = conn.execute(
                "DELETE FROM explore_leads WHERE outbound_date < ?", (today,)
            ).rowcount
            sweeps = conn.execute(
                "DELETE FROM sweep_runs WHERE started_at < ?", (cutoff,)
            ).rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        files = 0
        for r in doomed:
            if not r["raw_path"]:
                continue
            try:
                Path(r["raw_path"]).unlink(missing_ok=True)
                files += 1
            except OSError:  # a locked/readonly file must not fail the prune
                log.warning("could not delete raw snapshot file %s", r["raw_path"])
        summary = {
            "snapshots": len(ids), "flights": flights, "files": files,
            "current_departed": departed, "leads_departed": leads, "sweep_runs": sweeps,
        }
        log.info("prune (retention %dd): %s", retention_days, summary)
        return summary
