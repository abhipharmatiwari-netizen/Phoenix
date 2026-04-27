-- Runtime state tables (extended) not covered by migrations 001–012.
--
-- These tables were previously created via _ensure_schema() inside class
-- __init__ methods, causing fresh-DB startups to work only after the first
-- runtime initialisation. This migration creates them upfront so that
-- schema_guard.py strict mode passes on a completely blank database.
--
-- Tables included:
--   circuit_breaker_state  — PostgresCircuitBreakerStateStore (app/risk/circuit_breaker_store.py)
--   pnl_snapshots          — PostgresPnLStateStore (app/pnl/state_store.py)
--   profit_lock_state      — PostgresProfitLockBackend (app/pnl/profit_lock.py)

-- Circuit-breaker persistence (scope_id is the HUB_INSTANCE_NAME-qualified partition key)
CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    scope_id   TEXT        PRIMARY KEY,
    state_json TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- PnL snapshots (realized + unrealized per tenant / broker account / strategy)
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    tenant_id            TEXT             NOT NULL,
    broker_account_id    TEXT             NOT NULL,
    strategy_id          TEXT             NOT NULL,
    realized_pnl         DOUBLE PRECISION NOT NULL DEFAULT 0,
    unrealized_pnl       DOUBLE PRECISION NOT NULL DEFAULT 0,
    gross_exposure       DOUBLE PRECISION NOT NULL DEFAULT 0,
    as_of                TIMESTAMPTZ,
    session_date         DATE,
    freshness_updated_at TIMESTAMPTZ,
    freshness_source     TEXT,
    updated_at           TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, broker_account_id, strategy_id)
);

-- Profit-lock state (trailing lock once daily target is reached)
CREATE TABLE IF NOT EXISTS profit_lock_state (
    tenant_id            TEXT             NOT NULL,
    broker_account_id    TEXT             NOT NULL,
    last_reset_date      TEXT             NOT NULL,
    target_reached       BOOLEAN          NOT NULL DEFAULT FALSE,
    peak_total_pnl       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    last_update_ts       TEXT,
    last_exit_attempt_ts TEXT,
    updated_at           TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, broker_account_id)
);
