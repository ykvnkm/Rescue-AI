CREATE USER airflow WITH PASSWORD 'airflow';
CREATE DATABASE airflow OWNER airflow;

CREATE USER rescue_app WITH PASSWORD 'rescue_app';
CREATE DATABASE rescue_app OWNER rescue_app;

CREATE USER postgres_exporter WITH PASSWORD 'postgres_exporter';
GRANT CONNECT ON DATABASE postgres TO postgres_exporter;

\c postgres
GRANT pg_monitor TO postgres_exporter;

\c rescue_app
CREATE TABLE IF NOT EXISTS batch_mission_runs (
  run_key TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  reason TEXT NULL,
  report_uri TEXT NULL,
  debug_uri TEXT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
