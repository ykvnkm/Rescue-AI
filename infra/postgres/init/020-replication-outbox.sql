-- Transactional outbox for the hybrid deployment profile (ADR-0007 §3).
--
-- Local-first writes append a row here in the same transaction that
-- writes to the local domain tables. A separate `sync-worker` process
-- drains pending rows into the remote Postgres + S3, marking them
-- `synced` on success or incrementing `attempts` on failure.
--
-- Idempotency is enforced via `idempotency_key`: the remote side does
-- ON CONFLICT (idempotency_key) DO UPDATE so re-sending the same row
-- after a partial network loss never creates duplicates.
CREATE TABLE IF NOT EXISTS replication_outbox (
    id              BIGSERIAL PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    operation       TEXT NOT NULL,
    payload_json    JSONB NOT NULL,
    local_path      TEXT,
    s3_bucket       TEXT,
    s3_key          TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_replication_outbox_status_id
    ON replication_outbox (status, id);
