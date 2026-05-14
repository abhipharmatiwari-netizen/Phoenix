-- Migration 020: Strategy parameter candidate queue.
--
-- Issue #271 (epic #270): the nightly parameter optimizer writes its
-- top-N parameter sets here as ``status='pending'`` rows. Live
-- ``strategy_configs.params`` is NEVER mutated by the optimizer — a
-- separate human-approved promotion (admin API, issue #275) is the
-- only writer of approved candidates into ``strategy_configs``.
--
-- One row per (strategy_config_id, optimizer run, parameter set).
-- ``backtest_window`` records the exact date range pulled from
-- ``indicator_bars`` so the metrics are reproducible. ``optimizer_version``
-- is the optimizer image's git SHA so a bad run can be traced back to
-- the code revision that produced it.
--
-- ``status`` lifecycle:
--   pending     -> approved | rejected | superseded
--   approved    -> promoted          (set by admin API at the moment it
--                                     writes the new params into
--                                     ``strategy_configs``)
--   rejected    -> (terminal)
--   superseded  -> (terminal; set by a later optimizer run that produced
--                  an identical params hash for the same strategy_config_id)
--
-- The FK to ``strategy_configs`` is RESTRICT (no cascade). If a
-- ``strategy_configs`` row is being deleted, refuse the delete unless
-- the operator first archives or cleans up its candidates — keeps the
-- audit trail intact for a real-money system.

CREATE TABLE IF NOT EXISTS public.strategy_config_candidates (
    candidate_id        TEXT        PRIMARY KEY,
    strategy_config_id  TEXT        NOT NULL
                                    REFERENCES public.strategy_configs(strategy_config_id),
    params              JSONB       NOT NULL,
    metrics             JSONB       NOT NULL,
    backtest_window     DATERANGE   NOT NULL,
    optimizer_version   TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at         TIMESTAMPTZ,
    reviewed_by         TEXT,
    status              TEXT        NOT NULL DEFAULT 'pending'
                                    CHECK (status IN (
                                        'pending',
                                        'approved',
                                        'rejected',
                                        'promoted',
                                        'superseded'
                                    ))
);

CREATE INDEX IF NOT EXISTS idx_strategy_config_candidates_cfg_status
    ON public.strategy_config_candidates (strategy_config_id, status);

CREATE INDEX IF NOT EXISTS idx_strategy_config_candidates_created_at
    ON public.strategy_config_candidates (created_at DESC);

COMMENT ON TABLE public.strategy_config_candidates IS
    'Optimizer-proposed parameter sets pending human review (issue #271, '
    'epic #270). Live strategy_configs.params is only mutated by the '
    'admin-approval API, never directly by the optimizer.';

COMMENT ON COLUMN public.strategy_config_candidates.params IS
    'Parameter set proposed by the optimizer. Schema matches the target '
    'strategy''s known param keys (validated by the admin approval API '
    'before promotion into strategy_configs).';

COMMENT ON COLUMN public.strategy_config_candidates.metrics IS
    'Optimizer-computed metrics for this candidate: composite score, '
    'win_rate, total_pnl, max_drawdown, profit_factor, n_trades, plus '
    'walk-forward fold scores and out-of-sample holdout score (issue '
    '#273). Stored verbatim from BacktestMetrics.to_dict().';

COMMENT ON COLUMN public.strategy_config_candidates.backtest_window IS
    'Inclusive date range of indicator_bars rows the optimizer scored '
    'this candidate against. Reproducibility: re-running the optimizer '
    'against the same window with the same optimizer_version should '
    'produce comparable metrics.';

COMMENT ON COLUMN public.strategy_config_candidates.optimizer_version IS
    'Git SHA of the optimizer image (env IMAGE_TAG or `git rev-parse '
    'HEAD`). Used to trace candidates back to the optimizer code '
    'revision that produced them when investigating bad runs.';

COMMENT ON COLUMN public.strategy_config_candidates.status IS
    'Lifecycle: pending -> approved|rejected|superseded; approved -> '
    'promoted (set by admin API at the moment it writes new params '
    'into strategy_configs).';
