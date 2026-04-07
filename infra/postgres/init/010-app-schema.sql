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

CREATE TABLE IF NOT EXISTS batch_mission_runs (
    run_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    reason TEXT NULL,
    report_uri TEXT NULL,
    debug_uri TEXT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Summary metrics for each (ds, mission, model, code) run of the batch ML pipeline.
-- Written at the end of the pipeline by the `publish` stage; one row per
-- (ds, mission_id, model_version, code_version). Re-runs upsert via ON CONFLICT
-- so `updated_at` reflects the latest successful execution (idempotency signal).
CREATE TABLE IF NOT EXISTS batch_pipeline_metrics (
    ds               DATE NOT NULL,
    mission_id       TEXT NOT NULL,
    model_version    TEXT NOT NULL,
    code_version     TEXT NOT NULL,
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
    gt_available     BOOLEAN NOT NULL,
    validate_passed  BOOLEAN NOT NULL,
    inference_status TEXT NOT NULL,
    inference_run_key TEXT NOT NULL,
    dataset_uri      TEXT NOT NULL,
    model_uri        TEXT NOT NULL,
    validation_uri   TEXT NOT NULL,
    inference_uri    TEXT NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ds, mission_id, model_version, code_version)
);
CREATE INDEX IF NOT EXISTS ix_batch_pipeline_metrics_ds
    ON batch_pipeline_metrics (ds);
