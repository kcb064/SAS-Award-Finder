-- Phase 2: per-watch state for trip-level alerting, plus alert-delivery indexes.
--
-- Watch alerting compares each sweep's re-paired trips against the watch's LAST known state
-- (was there a bookable trip? at what best points? was it voucher-eligible?), so those live on
-- the watch row itself — they must survive restarts and don't belong in award_current.

ALTER TABLE watches ADD COLUMN last_run_at TEXT;
ALTER TABLE watches ADD COLUMN last_status TEXT;              -- 'ok' | 'failed'
ALTER TABLE watches ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE watches ADD COLUMN had_bookable INTEGER NOT NULL DEFAULT 0;
ALTER TABLE watches ADD COLUMN best_points INTEGER;           -- best trip points_total last check
ALTER TABLE watches ADD COLUMN had_voucher INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_alerts_delivery ON alerts(delivered_at, created_at);
CREATE INDEX idx_watches_enabled ON watches(enabled, origin, destination);
