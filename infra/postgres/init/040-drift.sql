-- Drift detection (ADR-0008 §4.7, ML design doc §2.7).
--
-- Две таблицы:
--   * drift_reference — эталонные гистограммы 4 признаков для одной
--     reference-миссии. Создаётся один раз командой
--     `python -m rescue_ai.interfaces.cli.batch \
--          --stage compute_drift_reference --ds X --mission-id Y`.
--     При смене модели / re-baseline создаётся новая запись;
--     `current` помечает активную через PARTIAL UNIQUE INDEX.
--   * drift_observations — наблюдения PSI/CSI на каждый ds.
--     Заполняется автоматически stage'ом compute_drift в daily DAG.

CREATE TABLE IF NOT EXISTS drift_reference (
    reference_id     TEXT PRIMARY KEY,
    mission_id       TEXT NOT NULL,
    ds               DATE NOT NULL,
    -- Гистограммы 4 признаков: confidence детекций, площадь bbox
    -- относительно кадра, aspect ratio bbox, mean brightness кадра.
    -- Хранятся как нормализованные доли (sum == 1.0); количество
    -- бинов фиксировано в коде (DRIFT_BINS=10).
    confidence_hist  DOUBLE PRECISION[] NOT NULL,
    bbox_area_hist   DOUBLE PRECISION[] NOT NULL,
    bbox_ratio_hist  DOUBLE PRECISION[] NOT NULL,
    brightness_hist  DOUBLE PRECISION[] NOT NULL,
    n_samples        INTEGER NOT NULL,
    -- Edges бинов — общие для confidence и brightness (0..1), для
    -- bbox_area (0..1, нормализовано) и bbox_ratio (0..5+). Реальные
    -- значения задаёт application/drift.py.
    confidence_edges DOUBLE PRECISION[] NOT NULL,
    bbox_area_edges  DOUBLE PRECISION[] NOT NULL,
    bbox_ratio_edges DOUBLE PRECISION[] NOT NULL,
    brightness_edges DOUBLE PRECISION[] NOT NULL,
    model_version    TEXT NOT NULL,
    is_current       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Только один активный reference в каждый момент времени. Новая запись
-- compute_drift_reference сначала снимает is_current со старых,
-- потом INSERT с is_current=true.
CREATE UNIQUE INDEX IF NOT EXISTS ux_drift_reference_current
    ON drift_reference (is_current)
    WHERE is_current = TRUE;

CREATE TABLE IF NOT EXISTS drift_observations (
    ds               DATE PRIMARY KEY,
    reference_id     TEXT NOT NULL REFERENCES drift_reference(reference_id),
    psi_confidence   DOUBLE PRECISION NOT NULL,
    csi_bbox_area    DOUBLE PRECISION NOT NULL,
    csi_bbox_ratio   DOUBLE PRECISION NOT NULL,
    csi_brightness   DOUBLE PRECISION NOT NULL,
    -- Флаг рассчитывается на стороне приложения по формуле 2.12:
    --   drift_flag = (psi_confidence > 0.2) OR (max(csi_*) > 0.2).
    -- Дублируем в БД, чтобы Prometheus-gauge не пересчитывал на лету.
    drift_flag       BOOLEAN NOT NULL,
    n_samples        INTEGER NOT NULL,
    n_missions       INTEGER NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_drift_observations_updated_at
    ON drift_observations (updated_at DESC);
