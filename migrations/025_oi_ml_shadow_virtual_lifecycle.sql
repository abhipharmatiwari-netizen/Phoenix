-- Migration 025: Complete dry-run OI/ML shadow virtual lifecycle evidence.
--
-- The OI/ML shadow sidecar remains dry-run only. These columns account for
-- virtual fill, virtual exit, flat-by-cutoff, and realized paper PnL evidence
-- without creating any live order queue.

ALTER TABLE public.oi_ml_shadow_order_intents
    ADD COLUMN IF NOT EXISTS virtual_entry_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS virtual_entry_credit_points NUMERIC(14, 6),
    ADD COLUMN IF NOT EXISTS virtual_exit_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS virtual_exit_debit_points NUMERIC(14, 6),
    ADD COLUMN IF NOT EXISTS virtual_flat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS virtual_exit_reason TEXT,
    ADD COLUMN IF NOT EXISTS realized_pnl_rupees NUMERIC(14, 2),
    ADD COLUMN IF NOT EXISTS lifecycle_events JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE public.oi_ml_shadow_order_intents
    DROP CONSTRAINT IF EXISTS oi_ml_shadow_order_intents_status_check;

ALTER TABLE public.oi_ml_shadow_order_intents
    ADD CONSTRAINT oi_ml_shadow_order_intents_status_check
    CHECK (status IN (
        'STAGED',
        'VIRTUAL_FILLED',
        'FLAT',
        'REJECTED',
        'EXPIRED',
        'CANCELLED'
    ));

CREATE INDEX IF NOT EXISTS idx_oi_ml_shadow_intents_status_time
    ON public.oi_ml_shadow_order_intents (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_oi_ml_shadow_intents_lifecycle_events
    ON public.oi_ml_shadow_order_intents USING GIN (lifecycle_events);

COMMENT ON COLUMN public.oi_ml_shadow_order_intents.virtual_entry_at IS
    'Dry-run virtual fill timestamp. Not broker evidence.';

COMMENT ON COLUMN public.oi_ml_shadow_order_intents.virtual_exit_at IS
    'Dry-run virtual exit timestamp. Not broker evidence.';

COMMENT ON COLUMN public.oi_ml_shadow_order_intents.virtual_flat_at IS
    'Dry-run flat timestamp used for shadow promotion evidence only.';

COMMENT ON COLUMN public.oi_ml_shadow_order_intents.realized_pnl_rupees IS
    'Dry-run realized PnL computed from virtual entry credit and virtual exit debit.';

COMMENT ON COLUMN public.oi_ml_shadow_order_intents.lifecycle_events IS
    'Dry-run lifecycle events only: staged, virtual filled, virtual exited, flat.';
