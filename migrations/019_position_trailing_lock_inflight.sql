-- Migration 019: Persisted inflight markers for the trailing-lock engine.
--
-- Issue #251: trailing-lock duplicate-fill guards (introduced by PR #236 for
-- the 2026-05-08 NATURALGAS22MAY26265CE duplicate-fill incident) are kept
-- in-memory only. A process restart after a trailing-lock exit was submitted
-- but BEFORE the broker order reached a terminal state drops the only guard
-- against a duplicate exit submission for the same position. Persisting the
-- markers to Postgres keeps the guard intact across restarts.
--
-- One row per (tenant_id, broker_account_id, symbol). ``submitted_at`` is the
-- wall-clock UTC instant the trailing-lock submit was attempted; the engine
-- compares the age against ``POSITION_TRAILING_LOCK_INFLIGHT_MAX_SECONDS``
-- to decide when to auto-clear. Rows are deleted on broker terminal
-- confirmation (FILLED / CANCELLED / REJECTED / EXPIRED) or on synchronous
-- terminal response from the router.
--
-- Idempotent: matches the CREATE TABLE IF NOT EXISTS issued at runtime by
-- PostgresPositionTrailingLockInflightBackend.__init__ — kept here so the
-- table is present before the backend container starts and so the schema
-- is auditable alongside other migrations.

CREATE TABLE IF NOT EXISTS public.position_trailing_lock_inflight (
    tenant_id          TEXT        NOT NULL,
    broker_account_id  TEXT        NOT NULL,
    symbol             TEXT        NOT NULL,
    broker_order_id    TEXT,
    submitted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, broker_account_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_position_trailing_lock_inflight_account
    ON public.position_trailing_lock_inflight (broker_account_id);

COMMENT ON TABLE public.position_trailing_lock_inflight IS
    'Durable inflight markers for the trailing-lock duplicate-fill guard '
    '(issue #251). One row per (tenant, account, symbol) while a trailing-'
    'lock exit is awaiting broker terminal confirmation. Survives restarts.';
