-- Migration 006: Trade decision lineage + slippage records
-- PHX-AUD-002, PHX-EXEC-004

-- Trade decision lineage (PHX-AUD-002)
CREATE TABLE IF NOT EXISTS trade_decision_lineage (
    lineage_id          TEXT        NOT NULL,
    order_intent_id     TEXT        NOT NULL,
    tenant_id           TEXT        NOT NULL,
    account_id          TEXT        NOT NULL,
    symbol              TEXT        NOT NULL,
    strategy_id         TEXT        NOT NULL DEFAULT '',
    strategy_version    TEXT        NOT NULL DEFAULT '',
    signal_values       JSONB,
    signal_timestamp    TEXT,
    regime              TEXT        NOT NULL DEFAULT '',
    regime_confidence   NUMERIC(6,4) NOT NULL DEFAULT 0,
    risk_checks         JSONB,
    risk_approved       BOOLEAN     NOT NULL DEFAULT FALSE,
    risk_denial_reason  TEXT        NOT NULL DEFAULT '',
    execution_style     TEXT        NOT NULL DEFAULT 'MARKET',
    intended_price      NUMERIC(18,4),
    intended_qty        INTEGER     NOT NULL DEFAULT 0,
    side                TEXT        NOT NULL DEFAULT '',
    fill_price          NUMERIC(18,4),
    fill_qty            INTEGER     NOT NULL DEFAULT 0,
    slippage_bps        NUMERIC(10,2),
    final_state         TEXT        NOT NULL DEFAULT '',
    broker_order_id     TEXT        NOT NULL DEFAULT '',
    created_at          TEXT        NOT NULL,
    resolved_at         TEXT,
    PRIMARY KEY (lineage_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tdl_order_intent ON trade_decision_lineage (order_intent_id);
CREATE INDEX IF NOT EXISTS idx_tdl_strategy ON trade_decision_lineage (strategy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tdl_symbol   ON trade_decision_lineage (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tdl_tenant   ON trade_decision_lineage (tenant_id, created_at DESC);

-- Slippage records (PHX-EXEC-004)
CREATE TABLE IF NOT EXISTS slippage_records (
    record_id               TEXT        NOT NULL,
    order_intent_id         TEXT        NOT NULL,
    symbol                  TEXT        NOT NULL,
    side                    TEXT        NOT NULL,
    strategy_id             TEXT        NOT NULL DEFAULT '',
    account_id              TEXT        NOT NULL DEFAULT '',
    intended_qty            INTEGER     NOT NULL DEFAULT 0,
    fill_qty                INTEGER     NOT NULL DEFAULT 0,
    arrival_price           NUMERIC(18,4),
    fill_price              NUMERIC(18,4),
    slippage_bps            NUMERIC(10,2),
    fill_ratio              NUMERIC(8,6) NOT NULL DEFAULT 0,
    time_to_fill_seconds    NUMERIC(10,3),
    created_at              TEXT        NOT NULL,
    PRIMARY KEY (record_id)
);
CREATE INDEX IF NOT EXISTS idx_slippage_strategy ON slippage_records (strategy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_slippage_symbol   ON slippage_records (symbol, created_at DESC);
