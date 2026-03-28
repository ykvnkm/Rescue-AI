#!/bin/bash
# Platform bootstrap: create Airflow role & DB when env vars are present.
# Silently skips if POSTGRES_AIRFLOW_PASSWORD / POSTGRES_APP_USER / POSTGRES_APP_PASSWORD
# are not set (they are only needed for full platform with Airflow).

set -e

if [ -z "$POSTGRES_AIRFLOW_PASSWORD" ] || [ -z "$POSTGRES_APP_USER" ] || [ -z "$POSTGRES_APP_PASSWORD" ]; then
    echo "001-platform-bootstrap: optional vars not set, skipping (this is OK)."
    exit 0
fi

echo "001-platform-bootstrap: creating Airflow role and app user..."

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'airflow') THEN
        EXECUTE format('CREATE ROLE airflow LOGIN PASSWORD %L', '$POSTGRES_AIRFLOW_PASSWORD');
      ELSE
        EXECUTE format('ALTER ROLE airflow WITH LOGIN PASSWORD %L', '$POSTGRES_AIRFLOW_PASSWORD');
      END IF;

      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$POSTGRES_APP_USER') THEN
        EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', '$POSTGRES_APP_USER', '$POSTGRES_APP_PASSWORD');
      ELSE
        EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L', '$POSTGRES_APP_USER', '$POSTGRES_APP_PASSWORD');
      END IF;
    END
    \$\$;

    SELECT format('CREATE DATABASE %I OWNER airflow', 'airflow')
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'airflow')
    \gexec

    SELECT format('CREATE DATABASE %I OWNER %I', '$POSTGRES_APP_USER', '$POSTGRES_APP_USER')
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$POSTGRES_APP_USER')
    \gexec
EOSQL

echo "001-platform-bootstrap: done."
