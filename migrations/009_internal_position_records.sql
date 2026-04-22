-- Migration 009: Durable authoritative internal position records
-- Architecture s.3.3 - persists OrderLifecycleService._position_records so
-- a restart can restore authoritative position state before broker reconciliation.

CREATE TABLE IF NOT EXISTS internal_position_records (
    scope_key            TEXT NOT NULL PRIMARY KEY,
    position_id          TEXT NOT NULL,
    tenant_id            TEXT NOT NULL,
    account_id           TEXT NOT NULL,
    strategy_id          TEXT NOT NULL,
    ownership_key        TEXT NOT NULL DEFAULT '',
    contract_key         TEXT NOT NULL DEFAULT '',
    side                 TEXT NOT NULL DEFAULT '',
    net_qty              DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    filled_qty_open      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    filled_qty_close     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    avg_open_price       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    avg_close_price      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    realized_pnl         DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    unrealized_pnl       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    position_state       TEXT NOT NULL DEFAULT 'NONE',
    state_reason         TEXT NOT NULL DEFAULT '',
    opened_by_order_id   TEXT NOT NULL DEFAULT '',
    last_evidence_at     TIMESTAMPTZ,
    last_reconciled_at   TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Partial index covering only non-terminal records - the common query path.
CREATE INDEX IF NOT EXISTS idx_internal_position_records_active
    ON internal_position_records (position_state)
    WHERE position_state NOT IN ('FLAT', 'NONE');
