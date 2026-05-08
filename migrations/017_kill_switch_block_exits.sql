-- Migration 017: Kill switch block-exits flag (issue #220)
--
-- Adds a `block_exits` column to ``kill_switch_state`` so the trip API can
-- choose between:
--   - SOFT trip (block_exits=FALSE, default): block new entries; exit
--     orders that reduce exposure remain allowed. Used by the legacy
--     daily-loss / drawdown auto-trip path so trailing-lock and operator
--     manual flatten can still close exposure.
--   - HARD trip (block_exits=TRUE): block ALL orders including exit orders.
--     Operator-initiated panic stop. Requires a separate manual-flatten
--     endpoint for any subsequent exposure reduction.
--
-- Backwards compatible: existing rows default to FALSE which preserves the
-- current behaviour (exits bypass the interceptor when the switch is tripped).

ALTER TABLE kill_switch_state
    ADD COLUMN IF NOT EXISTS block_exits BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN kill_switch_state.block_exits IS
    'Issue #220: when TRUE, the GlobalKillSwitchInterceptor rejects exit '
    'orders too while this record is TRIPPED / CLEAR_PENDING. Default FALSE '
    'preserves the historical "block entries only" behaviour.';
