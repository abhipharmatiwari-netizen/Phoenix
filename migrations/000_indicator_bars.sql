-- Baseline indicator_bars table creation.
--
-- This migration must run BEFORE 003_exclusive_nifty_ce_buy_private_ema.sql which
-- ALTERs this table. Previously the table was created at runtime by bar_persister.py,
-- making migration 003 fail on a blank database.
--
-- bar_persister.py continues to issue ADD COLUMN IF NOT EXISTS statements for
-- ema_20..di_spread and exclusive_nifty_ce_buy_ema20_30s at startup, which are
-- idempotent against this baseline schema.

CREATE TABLE IF NOT EXISTS indicator_bars (
    id               BIGSERIAL PRIMARY KEY,
    ts_start         TIMESTAMPTZ,
    ts_end           TIMESTAMPTZ,
    label            TEXT,
    timeframe_seconds INTEGER,
    o                DOUBLE PRECISION,
    h                DOUBLE PRECISION,
    l                DOUBLE PRECISION,
    c                DOUBLE PRECISION,
    atr              DOUBLE PRECISION,
    rsi              DOUBLE PRECISION,
    macd             DOUBLE PRECISION,
    macd_signal      DOUBLE PRECISION,
    macd_hist        DOUBLE PRECISION,
    ema_20           DOUBLE PRECISION,
    ema_30           DOUBLE PRECISION,
    ema_50           DOUBLE PRECISION,
    exclusive_nifty_ce_buy_ema20_30s DOUBLE PRECISION,
    adx              DOUBLE PRECISION,
    plus_di          DOUBLE PRECISION,
    minus_di         DOUBLE PRECISION,
    di_spread        DOUBLE PRECISION
);

CREATE UNIQUE INDEX IF NOT EXISTS indicator_bars_unique_idx
    ON indicator_bars (label, timeframe_seconds, ts_start);
