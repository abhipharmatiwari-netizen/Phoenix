-- Migration 021: 1-minute option-chain snapshots for OI/ML strategies.
--
-- This table stores vendor-normalized option quotes used by the intraday
-- OI/ML CE-seller pipeline. Rows are keyed by decision-time snapshot,
-- contract identity, and provider so repeated ingestion is idempotent.
--
-- ``quality_flags`` is deliberately persisted with the quote. Incomplete
-- provider rows may still be useful for audits and backfill diagnostics, but
-- live strategy gates must treat rows with hard flags as unusable for entry.

CREATE TABLE IF NOT EXISTS public.option_chain_1m (
    snapshot_ts     TIMESTAMPTZ NOT NULL,
    source_ts       TIMESTAMPTZ,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    underlying      TEXT        NOT NULL,
    expiry          DATE        NOT NULL,
    strike          INTEGER     NOT NULL,
    option_type     TEXT        NOT NULL CHECK (option_type IN ('CE', 'PE')),
    trading_symbol  TEXT        NOT NULL,
    exchange        TEXT        NOT NULL,
    symbol_token    TEXT,
    oi              BIGINT,
    volume          BIGINT,
    iv              NUMERIC(12, 6),
    bid             NUMERIC(14, 6),
    ask             NUMERIC(14, 6),
    ltp             NUMERIC(14, 6),
    underlying_ltp  NUMERIC(14, 6),
    vix             NUMERIC(12, 6),
    provider        TEXT        NOT NULL,
    raw_hash        TEXT,
    quality_flags   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (snapshot_ts, underlying, expiry, strike, option_type, provider)
);

CREATE INDEX IF NOT EXISTS idx_option_chain_1m_underlying_time
    ON public.option_chain_1m (underlying, snapshot_ts DESC);

CREATE INDEX IF NOT EXISTS idx_option_chain_1m_expiry_strike
    ON public.option_chain_1m (underlying, expiry, strike, option_type, snapshot_ts DESC);

CREATE INDEX IF NOT EXISTS idx_option_chain_1m_flags
    ON public.option_chain_1m USING GIN (quality_flags);

COMMENT ON TABLE public.option_chain_1m IS
    'Normalized 1-minute option-chain snapshots for OI/ML intraday strategies. '
    'Live entry gates must reject rows with hard quality_flags.';

COMMENT ON COLUMN public.option_chain_1m.snapshot_ts IS
    'Decision-time snapshot bucket. Feature builders may only use rows with '
    'snapshot_ts <= the candidate decision time.';

COMMENT ON COLUMN public.option_chain_1m.source_ts IS
    'Provider timestamp when available. Used to detect stale vendor payloads.';

COMMENT ON COLUMN public.option_chain_1m.provider IS
    'Normalized data source id, such as angel, truedata, or gdfl.';

COMMENT ON COLUMN public.option_chain_1m.quality_flags IS
    'Data-quality diagnostics from the ingestion adapter. Hard flags include '
    'missing_required_fields, missing_symbol_token, invalid_option_type, '
    'bad_bid_ask, and stale_source_seconds.';
