CREATE USER airflow WITH PASSWORD 'airflow';
CREATE DATABASE airflow OWNER airflow;

CREATE USER rescue_app WITH PASSWORD 'rescue_app';
CREATE DATABASE rescue_app OWNER rescue_app;

CREATE USER postgres_exporter WITH PASSWORD 'postgres_exporter';
GRANT CONNECT ON DATABASE postgres TO postgres_exporter;

\c postgres
GRANT pg_monitor TO postgres_exporter;
