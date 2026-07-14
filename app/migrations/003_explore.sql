-- Phase 3 (Explore): per-destination interest weight + lead upsert identity.

-- How much Kevin cares about a destination: 0 = hide from Explore / never sweep,
-- 1 = normal (default), 2 = high, 3 = must-track. Multiplies the availability score.
ALTER TABLE airports ADD COLUMN interest INTEGER NOT NULL DEFAULT 1;

-- A lead's identity is (origin, destination, cabin, month) — one best round-trip per bucket.
-- The unique index makes lead refreshes idempotent upserts; it also covers the lookups the old
-- non-unique index served, so drop that one.
DROP INDEX idx_explore_leads_route;
CREATE UNIQUE INDEX idx_explore_leads_bucket ON explore_leads(origin, destination, cabin, month);
