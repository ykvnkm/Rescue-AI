-- Создать отдельную базу данных airflow для метаданных Apache Airflow.
--
-- Apache Airflow ведёт свою таблицу dag_run, task_instance, xcom и т.д.
-- В дипломном стенде проще держать её в том же Postgres-инстансе, что
-- и приложение, но в отдельной БД, чтобы:
--   - схемы приложения и Airflow не смешивались;
--   - airflow-migrate не трогал приложения и наоборот.
--
-- Cloud-деплой использует отдельный managed Postgres для Airflow
-- (см. ADR-0008 §4), поэтому этот init-файл там не применяется.
CREATE DATABASE airflow OWNER rescue_ai;
GRANT ALL PRIVILEGES ON DATABASE airflow TO rescue_ai;
