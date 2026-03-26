\set ON_ERROR_STOP on
\getenv airflow_password POSTGRES_AIRFLOW_PASSWORD
\getenv app_user POSTGRES_APP_USER
\getenv app_password POSTGRES_APP_PASSWORD

DO
$$
BEGIN
  IF coalesce(length(trim(:'airflow_password')), 0) = 0 THEN
    RAISE EXCEPTION 'POSTGRES_AIRFLOW_PASSWORD is required';
  END IF;
  IF coalesce(length(trim(:'app_user')), 0) = 0 THEN
    RAISE EXCEPTION 'POSTGRES_APP_USER is required';
  END IF;
  IF coalesce(length(trim(:'app_password')), 0) = 0 THEN
    RAISE EXCEPTION 'POSTGRES_APP_PASSWORD is required';
  END IF;
END
$$;

DO
$$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'airflow') THEN
    EXECUTE format('CREATE ROLE airflow LOGIN PASSWORD %L', :'airflow_password');
  ELSE
    EXECUTE format('ALTER ROLE airflow WITH LOGIN PASSWORD %L', :'airflow_password');
  END IF;

  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app_user') THEN
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password');
  ELSE
    EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'app_user', :'app_password');
  END IF;
END
$$;

SELECT format('CREATE DATABASE %I OWNER airflow', 'airflow')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'airflow')
\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'app_user', :'app_user')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'app_user')
\gexec

\connect :app_user
\ir 010-app-schema.sql
