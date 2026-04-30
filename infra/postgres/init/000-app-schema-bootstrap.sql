-- Bootstrap для локального Postgres (offline / hybrid профили).
--
-- Cloud-БД (Supabase, ADR-0007) исторически держит таблицы в схеме `app`,
-- и репозитории в `rescue_ai/interfaces/cli/online.py` подключаются с
-- `search_path = app`. На локальном Postgres дефолтная схема — `public`,
-- из-за чего init-скрипты кладут таблицы туда, а код их не находит.
--
-- Этот файл сначала по приоритету (00-) создаёт схему `app` и
-- переключает search_path так, чтобы все последующие миграции
-- (010-, 011-, 020-) клали DDL прямо в `app`. Cloud-деплой не
-- затрагивается — там этот init-скрипт не выполняется (volume
-- инициализирует Supabase, не наш контейнер).
CREATE SCHEMA IF NOT EXISTS app;

-- Сделать `app` дефолтным для роли rescue, чтобы все CREATE TABLE
-- в последующих init-скриптах ушли в неё, и чтобы коннекты api
-- видели её первой.
ALTER ROLE CURRENT_USER SET search_path TO app, public;

-- Применить и для текущей сессии (init-скрипты выполняются одной
-- сессией psql; без этого следующий 010-app-schema.sql использует
-- старый search_path).
SET search_path TO app, public;
