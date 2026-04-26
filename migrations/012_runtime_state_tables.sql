-- Runtime state tables required by schema_guard.py at startup.
--
-- These three tables were previously created via _ensure_schema() inside
-- class __init__ methods called after the schema guard check, causing a
-- timing failure on a fresh database: the schema guard ran in strict mode
-- before HubRuntime was constructed, found the tables absent, and failed
-- startup.
--
-- Creating them here ensures that after running migrations the schema guard
-- passes on a completely blank database without the backend having run first.

-- Durable trade processed markers (idempotency for fill events across restarts)
-- Schema matches PostgresProcessedTradeStore._ensure_schema() in
-- app/orders/trade_processed_store.py:91-107.
CREATE TABLE IF NOT EXISTS trade_processed_markers (
    broker_account_id  TEXT        NOT NULL,
    broker_order_id    TEXT        NOT NULL,
    tenant_id          TEXT,
    strategy_id        TEXT,
    hub_order_id       TEXT,
    symbol             TEXT,
    side               TEXT,
    processed_at       TIMESTAMPTZ NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (broker_account_id, broker_order_id)
);

-- EOD exit state (tracks last exit date per account to prevent duplicate EOD exits)
-- Schema matches PostgresEODStateStore._ensure_schema() in
-- app/hub/eod_state.py:99.
CREATE TABLE IF NOT EXISTS eod_states (
    tenant_id          TEXT        NOT NULL,
    broker_account_id  TEXT        NOT NULL,
    last_exit_date     DATE,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, broker_account_id)
);

-- Profit sweep state (tracks sweep count and cooldown per account)
-- Schema matches PostgresSweepStateStore._save_state() in
-- app/hub/sweep_state.py:343-352.
CREATE TABLE IF NOT EXISTS sweep_states (
    tenant_id          TEXT        NOT NULL,
    broker_account_id  TEXT        NOT NULL,
    sweep_count_today  INTEGER     NOT NULL DEFAULT 0,
    last_sweep_time    TIMESTAMPTZ,
    last_reset_date    DATE,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, broker_account_id)
);
