-- Add human-readable slug to missions (e.g. "2026-03-28/mission-1")
ALTER TABLE missions ADD COLUMN IF NOT EXISTS slug TEXT UNIQUE;

-- Backfill existing missions with slug based on created_at date
WITH numbered AS (
    SELECT
        mission_id,
        to_char(created_at, 'YYYY-MM-DD') AS date_str,
        ROW_NUMBER() OVER (
            PARTITION BY to_char(created_at, 'YYYY-MM-DD')
            ORDER BY created_at, mission_id
        ) AS seq
    FROM missions
    WHERE slug IS NULL
)
UPDATE missions m
SET slug = numbered.date_str || '/mission-' || numbered.seq
FROM numbered
WHERE m.mission_id = numbered.mission_id;
