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

-- Summary metrics for each (mission, model, code) of the batch ML pipeline.
-- Written by the `publish` stage; exactly ONE row per
-- (mission_id, model_version, code_version) — re-running the pipeline for the
-- same mission on a new ds upserts the row in place, so the table does not
-- grow linearly with #missions × #days. `ds` stores the date of the last
-- successful run (observable idempotency signal alongside `updated_at`).
CREATE TABLE IF NOT EXISTS batch_pipeline_metrics (
    mission_id       TEXT NOT NULL,
    model_version    TEXT NOT NULL,
    code_version     TEXT NOT NULL,
    ds               DATE NOT NULL,
    rows_total       INTEGER NOT NULL,
    rows_positive    INTEGER NOT NULL,
    rows_corrupted   INTEGER NOT NULL,
    train_count      INTEGER NOT NULL,
    val_count        INTEGER NOT NULL,
    samples_total    INTEGER NOT NULL,
    tp               INTEGER NOT NULL,
    tn               INTEGER NOT NULL,
    fp               INTEGER NOT NULL,
    fn               INTEGER NOT NULL,
    detector_errors  INTEGER NOT NULL,
    accuracy         DOUBLE PRECISION NOT NULL,
    recall           DOUBLE PRECISION NOT NULL DEFAULT 0,
    gt_available     BOOLEAN NOT NULL,
    validate_passed  BOOLEAN NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (mission_id, model_version, code_version)
);
ALTER TABLE batch_pipeline_metrics
    ADD COLUMN IF NOT EXISTS recall DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE batch_pipeline_metrics
    DROP COLUMN IF EXISTS inference_status;
ALTER TABLE batch_pipeline_metrics
    DROP COLUMN IF EXISTS inference_run_key;
ALTER TABLE batch_pipeline_metrics
    DROP COLUMN IF EXISTS inference_uri;
ALTER TABLE batch_pipeline_metrics
    DROP COLUMN IF EXISTS validation_report_json;
ALTER TABLE batch_pipeline_metrics
    DROP COLUMN IF EXISTS dataset_uri;
ALTER TABLE batch_pipeline_metrics
    DROP COLUMN IF EXISTS model_uri;
ALTER TABLE batch_pipeline_metrics
    DROP COLUMN IF EXISTS validation_uri;
-- Migrate existing deployments from the old (ds, mission, model, code) PK
-- to the new mission-level PK: keep the latest row per mission (by ds) and
-- drop the rest before adding the new constraint.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE table_name = 'batch_pipeline_metrics'
          AND constraint_type = 'PRIMARY KEY'
          AND constraint_name = 'batch_pipeline_metrics_pkey'
    ) THEN
        IF EXISTS (
            SELECT 1
            FROM information_schema.key_column_usage
            WHERE constraint_name = 'batch_pipeline_metrics_pkey'
              AND column_name = 'ds'
        ) THEN
            DELETE FROM batch_pipeline_metrics m
            USING (
                SELECT mission_id, model_version, code_version, MAX(ds) AS last_ds
                FROM batch_pipeline_metrics
                GROUP BY mission_id, model_version, code_version
            ) latest
            WHERE m.mission_id   = latest.mission_id
              AND m.model_version = latest.model_version
              AND m.code_version = latest.code_version
              AND m.ds <> latest.last_ds;
            ALTER TABLE batch_pipeline_metrics
                DROP CONSTRAINT batch_pipeline_metrics_pkey;
            ALTER TABLE batch_pipeline_metrics
                ADD PRIMARY KEY (mission_id, model_version, code_version);
        END IF;
    END IF;
END $$;
DROP TABLE IF EXISTS batch_mission_runs;
CREATE INDEX IF NOT EXISTS ix_batch_pipeline_metrics_ds
    ON batch_pipeline_metrics (ds);
