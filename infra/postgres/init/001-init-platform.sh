#!/usr/bin/env bash
set -euo pipefail

required_vars=(
  POSTGRES_AIRFLOW_PASSWORD
  POSTGRES_APP_USER
  POSTGRES_APP_PASSWORD
)

for var_name in "${required_vars[@]}"; do
  if [ -z "${!var_name:-}" ]; then
    echo "Missing required env var: ${var_name}" >&2
    exit 1
  fi
done

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
DO
\$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'airflow') THEN
    CREATE ROLE airflow LOGIN PASSWORD '${POSTGRES_AIRFLOW_PASSWORD}';
  ELSE
    ALTER ROLE airflow WITH LOGIN PASSWORD '${POSTGRES_AIRFLOW_PASSWORD}';
  END IF;

  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${POSTGRES_APP_USER}') THEN
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', '${POSTGRES_APP_USER}', '${POSTGRES_APP_PASSWORD}');
  ELSE
    EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L', '${POSTGRES_APP_USER}', '${POSTGRES_APP_PASSWORD}');
  END IF;
END
\$\$;

CREATE DATABASE airflow OWNER airflow;
SQL

psql -v ON_ERROR_STOP=0 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE DATABASE ${POSTGRES_APP_USER} OWNER ${POSTGRES_APP_USER};
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_APP_USER" <<SQL
CREATE TABLE IF NOT EXISTS batch_mission_runs (
  run_key TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  reason TEXT NULL,
  report_uri TEXT NULL,
  debug_uri TEXT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL
