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
--
-- PR #261 round-5 review P2 (migrations/019:29): include ``exit_side``
-- (TEXT NULL) and ``exit_broker_units`` (INTEGER NULL) here so that fresh
-- deployments where migrations OWN schema and the application role is
-- DML-only get the columns the backend immediately reads/writes via
-- ``_LIST_ALL`` / ``_UPSERT``. Without them, the runtime ALTER TABLE in
-- ``PostgresPositionTrailingLockInflightBackend.__init__`` is the only
-- thing creating the columns — and on a DML-only app role that ALTER
-- silently fails, after which every LIVE ``save_marker(..., fail_closed=True)``
-- aborts the trailing-lock exit. The bottom-of-file ``ALTER TABLE IF
-- EXISTS ... ADD COLUMN IF NOT EXISTS`` blocks make this migration safe
-- to apply on existing deployments that already created the table via
-- an older revision of this file.

CREATE TABLE IF NOT EXISTS public.position_trailing_lock_inflight (
    tenant_id          TEXT        NOT NULL,
    broker_account_id  TEXT        NOT NULL,
    symbol             TEXT        NOT NULL,
    broker_order_id    TEXT,
    submitted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exit_side          TEXT        NULL,
    exit_broker_units  INTEGER     NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, broker_account_id, symbol)
);

-- PR #261 round-5 review P2 (migrations/019:29): idempotent guard for
-- existing deployments that ran an earlier revision of this migration.
-- ``ADD COLUMN IF NOT EXISTS`` requires Postgres 9.6+; wrap with
-- ``ALTER TABLE IF EXISTS`` so a re-run on a not-yet-created table is
-- a no-op (defensive — the CREATE TABLE above runs first).
ALTER TABLE IF EXISTS public.position_trailing_lock_inflight
    ADD COLUMN IF NOT EXISTS exit_side          TEXT    NULL;
ALTER TABLE IF EXISTS public.position_trailing_lock_inflight
    ADD COLUMN IF NOT EXISTS exit_broker_units  INTEGER NULL;

CREATE INDEX IF NOT EXISTS idx_position_trailing_lock_inflight_account
    ON public.position_trailing_lock_inflight (broker_account_id);

COMMENT ON TABLE public.position_trailing_lock_inflight IS
    'Durable inflight markers for the trailing-lock duplicate-fill guard '
    '(issue #251). One row per (tenant, account, symbol) while a trailing-'
    'lock exit is awaiting broker terminal confirmation. Survives restarts.';

COMMENT ON COLUMN public.position_trailing_lock_inflight.exit_side IS
    'Exit order side captured at submission time (PR #261 round-3 P2). '
    'Used by the disappeared-symbol terminal-evidence fallback to match '
    'the right ledger row when broker_order_id is unknown.';
COMMENT ON COLUMN public.position_trailing_lock_inflight.exit_broker_units IS
    'Exit order quantity in BROKER UNITS (PR #261 round-3 P2). Stored in '
    'broker units (not lots) because the broker order snapshot reports '
    '``OrderStatus.quantity`` in broker units after the router resolves '
    '``broker_qty``.';
