CREATE TABLE IF NOT EXISTS missions (
    mission_id  TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    total_frames INTEGER NOT NULL,
    fps         DOUBLE PRECISION NOT NULL,
    completed_frame_id INTEGER,
    slug        TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_missions_status ON missions (status);

CREATE TABLE IF NOT EXISTS frame_events (
    mission_id       TEXT NOT NULL REFERENCES missions (mission_id) ON DELETE CASCADE,
    frame_id         INTEGER NOT NULL,
    ts_sec           DOUBLE PRECISION NOT NULL,
    image_uri        TEXT NOT NULL,
    gt_person_present BOOLEAN NOT NULL,
    gt_episode_id    TEXT,
    PRIMARY KEY (mission_id, frame_id)
);
CREATE INDEX IF NOT EXISTS ix_frame_events_mission_ts
    ON frame_events (mission_id, ts_sec);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id           TEXT PRIMARY KEY,
    mission_id         TEXT NOT NULL REFERENCES missions (mission_id) ON DELETE CASCADE,
    frame_id           INTEGER NOT NULL,
    ts_sec             DOUBLE PRECISION NOT NULL,
    image_uri          TEXT NOT NULL,
    people_detected    INTEGER NOT NULL,
    primary_bbox       JSONB NOT NULL,
    primary_score      DOUBLE PRECISION NOT NULL,
    primary_label      TEXT NOT NULL,
    primary_model_name TEXT NOT NULL,
    primary_explanation TEXT,
    detections         JSONB NOT NULL,
    status             TEXT NOT NULL,
    reviewed_by        TEXT,
    reviewed_at_sec    DOUBLE PRECISION,
    decision_reason    TEXT,
    FOREIGN KEY (mission_id, frame_id)
        REFERENCES frame_events (mission_id, frame_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_alerts_mission_ts
    ON alerts (mission_id, ts_sec);
CREATE INDEX IF NOT EXISTS ix_alerts_mission_status
    ON alerts (mission_id, status);

CREATE TABLE IF NOT EXISTS episodes (
    mission_id     TEXT NOT NULL REFERENCES missions (mission_id) ON DELETE CASCADE,
    episode_index  INTEGER NOT NULL,
    start_sec      DOUBLE PRECISION NOT NULL,
    end_sec        DOUBLE PRECISION NOT NULL,
    found_by_alert BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (mission_id, episode_index)
);
CREATE INDEX IF NOT EXISTS ix_episodes_mission_found
    ON episodes (mission_id, found_by_alert);

-- Fact table for the daily batch ML pipeline.
--
-- One row per (ds, mission_id, model_version) — one row per evaluated
-- mission, on the date the mission was created (`ds` is a Hive-style
-- partition key matching the S3 layout). Missions live in exactly one
-- partition: a mission created on 2026-04-09 produces one row for
-- `ds=2026-04-09`, regardless of how many times the DAG runs.
--
-- Written by the `publish_metrics` stage. Re-running the DAG for an
-- existing `ds` is idempotent via ON CONFLICT ... DO UPDATE: old rows
-- refresh in place, new missions insert. `updated_at` tracks the
-- wall-clock time of the last write and is the observable signal that
-- a rerun actually happened.
CREATE TABLE IF NOT EXISTS batch_pipeline_metrics (
    ds               DATE NOT NULL,
    mission_id       TEXT NOT NULL,
    model_version    TEXT NOT NULL,
    rows_total       INTEGER NOT NULL,
    rows_positive    INTEGER NOT NULL,
    rows_corrupted   INTEGER NOT NULL,
    evaluation_count INTEGER NOT NULL,
    tp               INTEGER NOT NULL,
    tn               INTEGER NOT NULL,
    fp               INTEGER NOT NULL,
    fn               INTEGER NOT NULL,
    detector_errors  INTEGER NOT NULL,
    accuracy         DOUBLE PRECISION NOT NULL,
    precision        DOUBLE PRECISION NOT NULL DEFAULT 0,
    recall           DOUBLE PRECISION NOT NULL DEFAULT 0,
    gt_available     BOOLEAN NOT NULL,
    validate_passed  BOOLEAN NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ds, mission_id, model_version)
);
CREATE INDEX IF NOT EXISTS ix_batch_pipeline_metrics_ds
    ON batch_pipeline_metrics (ds);

-- Daily roll-up over the fact table. Aggregates the per-mission rows
-- into one row per (ds, model_version) so dashboards can read drift
-- signals without re-implementing the aggregation.
CREATE OR REPLACE VIEW batch_daily_metrics AS
SELECT
    ds,
    model_version,
    COUNT(*)                              AS missions_total,
    SUM(rows_total)                       AS rows_total,
    SUM(rows_positive)                    AS rows_positive,
    SUM(rows_corrupted)                   AS rows_corrupted,
    SUM(evaluation_count)                 AS evaluation_count,
    SUM(tp)                               AS tp,
    SUM(tn)                               AS tn,
    SUM(fp)                               AS fp,
    SUM(fn)                               AS fn,
    SUM(detector_errors)                  AS detector_errors,
    CASE
        WHEN SUM(tp + tn + fp + fn) > 0
        THEN ROUND((SUM(tp + tn))::numeric / SUM(tp + tn + fp + fn), 4)
        ELSE NULL
    END                                    AS accuracy,
    CASE
        WHEN SUM(tp + fp) > 0
        THEN ROUND(SUM(tp)::numeric / SUM(tp + fp), 4)
        ELSE NULL
    END                                    AS precision,
    CASE
        WHEN SUM(tp + fn) > 0
        THEN ROUND(SUM(tp)::numeric / SUM(tp + fn), 4)
        ELSE NULL
    END                                    AS recall,
    MAX(updated_at)                       AS updated_at
FROM batch_pipeline_metrics
GROUP BY ds, model_version;
