-- Per-fill trade ledger.
--
-- Source of truth for the /me/trades dashboard endpoint when BigQuery is
-- not configured (i.e. the OCI-only deployment). Replaces the prior CSV
-- fallback at /app/logs/<date>/trades.csv, which suffered from schema
-- collision with the legacy strategy-cycle CSV writer in
-- multi_instrument_stream.py and had no atomicity / time-range query
-- support.
--
-- Written by OrderLifecycleService._emit_trade on every confirmed fill in
-- LIVE mode, alongside the existing pnl_engine.on_trade(...) call that
-- updates pnl_snapshots.realized_pnl.
--
-- PRIMARY KEY (broker_account_id, trade_id) provides natural idempotency:
-- a duplicate fill notification for the same broker_order_id is a no-op
-- via ON CONFLICT DO NOTHING.

CREATE TABLE IF NOT EXISTS public.trades (
    trade_id          TEXT             NOT NULL,
    broker_account_id TEXT             NOT NULL,
    tenant_id         TEXT             NOT NULL,
    strategy_id       TEXT             NOT NULL,
    hub_order_id      TEXT,
    symbol            TEXT             NOT NULL,
    side              TEXT             NOT NULL,
    qty               BIGINT           NOT NULL,
    price             DOUBLE PRECISION NOT NULL,
    trade_time        TIMESTAMPTZ      NOT NULL,
    realized_pnl      DOUBLE PRECISION,
    fees              DOUBLE PRECISION,
    entry_price       DOUBLE PRECISION,
    exit_price        DOUBLE PRECISION,
    entry_time        TIMESTAMPTZ,
    exit_time         TIMESTAMPTZ,
    execution_mode    TEXT,
    virtual           BOOLEAN          NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (broker_account_id, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_trades_tenant_time
    ON public.trades (tenant_id, trade_time DESC);
CREATE INDEX IF NOT EXISTS idx_trades_account_time
    ON public.trades (broker_account_id, trade_time DESC);
