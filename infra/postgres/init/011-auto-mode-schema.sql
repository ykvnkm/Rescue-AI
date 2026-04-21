-- Schema extension for automatic-mode missions (ADR-0006).
--
-- Applied after 010-app-schema.sql on a fresh Postgres volume. All DDL
-- is idempotent so the file can also be applied to an already-populated
-- production database as a one-off migration:
--
--     psql $DB_DSN -f 011-auto-mode-schema.sql
--
-- Changes:
--   1. `missions` gains a `mode` discriminator ('operator' | 'automatic').
--   2. Three satellite tables scoped to automatic missions.
--   3. Existing rows are backfilled to `operator` for safety, even though
--      the DEFAULT clause already covers this on modern Postgres.

ALTER TABLE missions
    ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'operator';

UPDATE missions SET mode = 'operator' WHERE mode IS NULL;

ALTER TABLE missions DROP CONSTRAINT IF EXISTS missions_mode_valid;
ALTER TABLE missions
    ADD CONSTRAINT missions_mode_valid
    CHECK (mode IN ('operator', 'automatic'));

CREATE INDEX IF NOT EXISTS ix_missions_mode ON missions (mode);


-- Per-mission trajectory emitted by the navigation engine.
--
-- ``seq`` is a per-mission monotonically increasing counter used as the
-- sort key; ``ts_sec`` aligns the point to the mission timeline.
-- ``frame_id`` is nullable because interpolated/filled points may not
-- correspond to a stored frame event.
CREATE TABLE IF NOT EXISTS auto_trajectory_points (
    mission_id TEXT NOT NULL REFERENCES missions (mission_id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    ts_sec     DOUBLE PRECISION NOT NULL,
    frame_id   INTEGER,
    x          DOUBLE PRECISION NOT NULL,
    y          DOUBLE PRECISION NOT NULL,
    z          DOUBLE PRECISION NOT NULL,
    source     TEXT NOT NULL,
    PRIMARY KEY (mission_id, seq),
    CONSTRAINT auto_trajectory_source_valid
        CHECK (source IN ('marker', 'optical_flow', 'fallback'))
);
CREATE INDEX IF NOT EXISTS ix_auto_trajectory_mission_ts
    ON auto_trajectory_points (mission_id, ts_sec);


-- Append-only audit log of automatic decisions. This is the counterpart
-- to operator review fields on `alerts`: for automatic missions those
-- fields stay NULL and the reasoning is captured here instead.
CREATE TABLE IF NOT EXISTS auto_decisions (
    decision_id TEXT PRIMARY KEY,
    mission_id  TEXT NOT NULL REFERENCES missions (mission_id) ON DELETE CASCADE,
    frame_id    INTEGER,
    ts_sec      DOUBLE PRECISION NOT NULL,
    kind        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT auto_decisions_kind_valid
        CHECK (kind IN ('alert_created', 'alert_suppressed'))
);
CREATE INDEX IF NOT EXISTS ix_auto_decisions_mission_ts
    ON auto_decisions (mission_id, ts_sec);


-- One snapshot per automatic mission of the configuration it started
-- with (navigation mode, detector, full NavigationTuning as JSON). Kept
-- separate from `missions` to avoid widening the shared row for data
-- only automatic missions need.
CREATE TABLE IF NOT EXISTS auto_mission_config (
    mission_id  TEXT PRIMARY KEY REFERENCES missions (mission_id) ON DELETE CASCADE,
    nav_mode    TEXT NOT NULL,
    detector    TEXT NOT NULL,
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT auto_mission_config_nav_mode_valid
        CHECK (nav_mode IN ('marker', 'no_marker', 'auto'))
);
