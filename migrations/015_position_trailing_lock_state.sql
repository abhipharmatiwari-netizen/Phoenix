-- Per-position trailing profit lock state.
--
-- Tracks the peak unrealized P&L of each open position (keyed by symbol)
-- since the position was first observed with non-zero qty. The hub-side
-- PositionTrailingLockEngine reads + upserts this row every periodic cycle
-- and triggers a single-position exit when the current unrealized P&L falls
-- below peak_unrealized_pnl * (1 - giveback_pct), provided peak has crossed
-- the configured arming floor.
--
-- Rows are deleted when a position closes (qty drops to 0).
--
-- Idempotent: matches the CREATE TABLE IF NOT EXISTS issued at runtime by
-- PostgresPositionTrailingLockBackend.__init__ — kept here so the table is
-- present before the backend container starts and so the schema is auditable
-- alongside other migrations.

CREATE TABLE IF NOT EXISTS public.position_trailing_lock_state (
    tenant_id            TEXT             NOT NULL,
    broker_account_id    TEXT             NOT NULL,
    symbol               TEXT             NOT NULL,
    peak_unrealized_pnl  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    armed                BOOLEAN          NOT NULL DEFAULT FALSE,
    opened_at            TEXT,
    last_update_ts       TEXT,
    last_exit_attempt_ts TEXT,
    updated_at           TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, broker_account_id, symbol)
);
