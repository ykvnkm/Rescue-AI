# Offline / Hybrid station deployment

Realisation of **ADR-0007**: same codebase as the cloud profile, all
dependencies local, optional outbox drain into remote Postgres/S3.

## Layout

```
infra/offline/
├── docker-compose.offline.yml   # postgres + minio + api (+ sync-worker)
├── .env.offline.example         # copy to .env.offline and edit
└── README.md                    # this file
```

The compose file reuses the canonical schema in
[`infra/postgres/init/`](../postgres/init/) — the same migrations the
cloud Postgres runs, including the `replication_outbox` table from
ADR-0007 §3.

## First start

1. Copy the env example and edit passwords:
   ```sh
   cp infra/offline/.env.offline.example infra/offline/.env.offline
   ```
2. Generate the mTLS material (one-time, stored on disk under
   `scripts/security/out/`):
   ```sh
   ./scripts/security/gen_ca.sh
   ./scripts/security/gen_rpi_cert.sh
   ./scripts/security/gen_client_cert.sh
   ```
3. Bring up the offline stack:
   ```sh
   docker compose -f infra/offline/docker-compose.offline.yml up -d
   ```
4. Verify:
   ```sh
   curl http://localhost:8000/health
   ```

## Switching to hybrid

When the station gets occasional connectivity, fill in the
`DEPLOYMENT_REMOTE_*` block in `.env.offline`, set
`DEPLOYMENT_MODE=hybrid`, and start the worker profile:

```sh
docker compose -f infra/offline/docker-compose.offline.yml \
    --profile hybrid up -d
```

The worker drains `replication_outbox` rows in
`DEPLOYMENT_SYNC_BATCH_SIZE` batches every
`DEPLOYMENT_SYNC_INTERVAL_SEC` seconds. Idempotency is enforced by
`idempotency_key` on the remote side — re-runs after partial network
loss never produce duplicates.

## What the cloud profile keeps

The root [`docker-compose.yml`](../../docker-compose.yml) is unchanged.
`DEPLOYMENT_MODE` defaults to `cloud` (see
[`rescue_ai/config.py`](../../rescue_ai/config.py)) so existing
deployments behave exactly as before.
