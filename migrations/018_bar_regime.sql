-- Persist dynamic_policy regime classification per indicator bar.
-- Classifier versions are kept side-by-side so future classifiers can be
-- replayed against historical bars without overwriting production decisions.

CREATE TABLE IF NOT EXISTS bar_regime (
    bar_id             BIGINT NOT NULL REFERENCES indicator_bars(id) ON DELETE CASCADE,
    regime             TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    signals            JSONB,
    classified_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bar_id, classifier_version)
);

CREATE INDEX IF NOT EXISTS bar_regime_classifier_ts
    ON bar_regime (classifier_version, classified_at DESC);

CREATE INDEX IF NOT EXISTS bar_regime_regime_classifier_idx
    ON bar_regime (regime, classifier_version);
