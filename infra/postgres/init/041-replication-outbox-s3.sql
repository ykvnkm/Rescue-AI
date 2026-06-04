-- Расширение таблицы replication_outbox для репликации S3-артефактов
-- из локального MinIO в удалённое S3 (ADR-0007 §3, диплом §3.5.1).
--
-- Существующие колонки s3_bucket / s3_key используются как
-- координаты НАЗНАЧЕНИЯ (куда копировать в remote S3); новые
-- source_s3_bucket / source_s3_key — координаты ИСТОЧНИКА
-- (откуда читать из локального MinIO).
--
-- Это позволяет sync-worker'у выполнять S3-to-S3 копирование
-- (GET из локального S3 → PUT в remote S3) без локального
-- shared-volume между api и sync-worker подами: каждый pod имеет
-- свой собственный путь к localMinio через ClusterIP, и sync-worker
-- получает creds локального MinIO через Vault Agent (см. KV-путь
-- secret/rescue-ai/sync-worker, поля SOURCE_S3_*).
--
-- Старое поле local_path остаётся в схеме для совместимости с
-- сценарием «файл с диска пода», но не используется новым кодом
-- репликации артефактов.

ALTER TABLE replication_outbox
    ADD COLUMN IF NOT EXISTS source_s3_bucket TEXT,
    ADD COLUMN IF NOT EXISTS source_s3_key    TEXT;
