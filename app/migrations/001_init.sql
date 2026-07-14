-- EuroBonus Award Finder — initial schema.
-- Phase 1 actively uses: airports, availability_snapshots, award_flights, award_current,
-- provider_calls. The watches / sweep_runs / alerts / explore_leads / cash_fares tables are
-- created now (Phases 2-4 fill them) so the schema version is stable and migrations don't churn.

-- Airport catalog, seeded from the network feed (destinations) plus configured home airports.
CREATE TABLE airports (
    code          TEXT PRIMARY KEY,          -- IATA, e.g. 'BOS'
    city_name     TEXT,
    country_name  TEXT,
    city_code     TEXT,
    lat           REAL,
    lng           REAL,
    flight_classes TEXT,                      -- JSON array of cabins the route is ever sold in
    region        TEXT,                       -- zone key for the points table (filled from config)
    image         TEXT,
    is_home       INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);

-- One row per fetched snapshot. Raw JSON lives on disk (raw_path) to keep the DB lean; metadata
-- here supports diffing ("was this a full ok sweep?") and pruning.
CREATE TABLE availability_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    origin        TEXT NOT NULL,
    scope         TEXT NOT NULL,              -- 'network' (all dests, outbound-only) | 'route' (one dest, both legs)
    destination   TEXT,                       -- NULL for network scope
    fetched_at    TEXT NOT NULL,              -- ISO8601 UTC
    status        TEXT NOT NULL,              -- 'ok' | 'partial' | 'failed'
    dest_count    INTEGER NOT NULL DEFAULT 0,
    byte_size     INTEGER NOT NULL DEFAULT 0,
    raw_path      TEXT                        -- path to the raw JSON on the data volume
);
CREATE INDEX idx_snapshots_origin_scope ON availability_snapshots(origin, scope, destination, fetched_at);

-- Append-only observation history: one row per (direction, date, origin, dest, cabin) per snapshot.
CREATE TABLE award_flights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    flight_key      TEXT NOT NULL,            -- sha1(direction|date|origin|dest|cabin)
    snapshot_id     INTEGER NOT NULL REFERENCES availability_snapshots(id) ON DELETE CASCADE,
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    direction       TEXT NOT NULL,            -- 'outbound' | 'inbound'
    flight_date     TEXT NOT NULL,            -- YYYY-MM-DD
    cabin           TEXT NOT NULL,            -- 'AG' | 'AP' | 'AB'
    seats           INTEGER NOT NULL,         -- seats in this cabin on this date
    seats_total     INTEGER NOT NULL,         -- availableSeatsTotal across cabins for the date
    is_sas_operated INTEGER NOT NULL DEFAULT 1,
    observed_at     TEXT NOT NULL,
    segments        TEXT                      -- JSON, reserved for later (flight numbers/connections)
);
CREATE INDEX idx_award_flights_key ON award_flights(flight_key);
CREATE INDEX idx_award_flights_route ON award_flights(origin, destination, direction, flight_date, cabin);
CREATE INDEX idx_award_flights_snapshot ON award_flights(snapshot_id);

-- Latest known state per flight_key: the anchor the diffing engine compares each new sweep against.
CREATE TABLE award_current (
    flight_key      TEXT PRIMARY KEY,
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    direction       TEXT NOT NULL,
    flight_date     TEXT NOT NULL,
    cabin           TEXT NOT NULL,
    seats           INTEGER NOT NULL,
    seats_total     INTEGER NOT NULL,
    is_sas_operated INTEGER NOT NULL DEFAULT 1,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    last_snapshot_id INTEGER REFERENCES availability_snapshots(id) ON DELETE SET NULL
);
CREATE INDEX idx_award_current_route ON award_current(origin, destination, direction, flight_date, cabin);

-- Daily budget / audit of SAS requests. One row per request attempt.
CREATE TABLE provider_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    call_date     TEXT NOT NULL,              -- YYYY-MM-DD (local) for budget bucketing
    provider      TEXT NOT NULL DEFAULT 'sas_direct',
    scope         TEXT NOT NULL,              -- 'network' | 'route'
    origin        TEXT NOT NULL,
    destination   TEXT,
    status        TEXT NOT NULL,              -- 'ok' | 'failed'
    http_status   INTEGER,
    byte_size     INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_provider_calls_date ON provider_calls(call_date);

-- ---- Forward-looking tables (Phases 2-4). Created now so migrations stay stable. ----

-- Background watches (Phase 2). Round-trip by default; one-way leaves the return window null.
CREATE TABLE watches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    label          TEXT,
    origin         TEXT NOT NULL,
    destination    TEXT NOT NULL,
    cabin          TEXT,                      -- NULL = any cabin
    trip_type      TEXT NOT NULL DEFAULT 'RT',-- 'RT' | 'OW'
    date_from      TEXT NOT NULL,             -- outbound window start (YYYY-MM-DD)
    date_to        TEXT NOT NULL,             -- outbound window end
    return_from    TEXT,                      -- inbound window start (NULL for OW)
    return_to      TEXT,                      -- inbound window end
    min_stay_days  INTEGER NOT NULL DEFAULT 2,
    max_stay_days  INTEGER NOT NULL DEFAULT 30,
    min_seats      INTEGER NOT NULL DEFAULT 1,
    sas_only       INTEGER NOT NULL DEFAULT 1,
    voucher_mode   INTEGER NOT NULL DEFAULT 0,-- 1 = hunt 2-for-1 (implies min_seats>=2, both legs)
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- Sweep bookkeeping (Phase 2): lets diffing know whether a sweep was fully 'ok' before it emits
-- negative diffs (closed / price-up).
CREATE TABLE sweep_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,             -- 'network' | 'route' | 'watch'
    origin        TEXT,
    destination   TEXT,
    status        TEXT NOT NULL,             -- 'ok' | 'partial' | 'failed'
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    notes         TEXT
);

-- Alerts log (Phase 2).
CREATE TABLE alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id       INTEGER REFERENCES watches(id) ON DELETE CASCADE,
    type           TEXT NOT NULL,            -- opened | price_drop | closed | voucher_pair | ops
    dedup_key      TEXT NOT NULL,
    title          TEXT NOT NULL,
    body           TEXT,
    outbound_date  TEXT,
    inbound_date   TEXT,
    cabin          TEXT,
    created_at     TEXT NOT NULL,
    delivered_at   TEXT,                      -- NULL until Apprise confirms send
    delivery_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX idx_alerts_dedup ON alerts(dedup_key);

-- Explore leads (Phase 3): best round-trip per route/month/cabin, a ranked cached view.
CREATE TABLE explore_leads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    origin         TEXT NOT NULL,
    destination    TEXT NOT NULL,
    cabin          TEXT NOT NULL,
    month          TEXT,                      -- YYYYMM bucket
    outbound_date  TEXT,
    inbound_date   TEXT,
    out_seats      INTEGER,
    in_seats       INTEGER,
    stay_days      INTEGER,
    points_total   INTEGER,
    taxes_total    REAL,
    cpp            REAL,
    cpp_voucher    REAL,
    voucher_eligible INTEGER NOT NULL DEFAULT 0,
    computed_at    TEXT NOT NULL
);
CREATE INDEX idx_explore_leads_route ON explore_leads(origin, destination, cabin, month);

-- Cash fares (Phase 4): for cpp math.
CREATE TABLE cash_fares (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    origin         TEXT NOT NULL,
    destination    TEXT NOT NULL,
    cabin          TEXT NOT NULL,
    trip_type      TEXT NOT NULL DEFAULT 'RT',
    price          REAL NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'USD',
    observed_at    TEXT NOT NULL,
    source         TEXT
);
CREATE INDEX idx_cash_fares_route ON cash_fares(origin, destination, cabin, trip_type);
