-- One-shot cleanup: clear the A1 ema20_strategy NG-22May-255-CE record stuck in
-- RECOVERY_PENDING / illegal_transition_blocked since 2026-05-07 ~14:30 UTC.
--
-- Root cause: a single close fill of 2,500 units on a 1,250-unit short open
-- (flip-the-trade) violated the partial_exit invariant filled_close <= filled_open
-- and parked the row non-terminal. The runner has been unable to refresh
-- positions for A1 ever since. Operator manually squared the broker side first.

BEGIN;

UPDATE internal_position_records
SET position_state     = 'FLAT',
    state_reason       = 'manual_recovery_post_flip_2026_05_07',
    last_reconciled_at = NOW(),
    updated_at         = NOW()
WHERE account_id = 'A1'
  AND scope_key  = $$tenant-1:A1:ema20_strategy:INTRADAY:('NG', '2026-05-22', '255', 'CE', 'INTRADAY')$$;

DELETE FROM position_ownership_ledger
WHERE broker_account_id = 'A1'
  AND underlying        = 'NG'
  AND expiry            = '2026-05-22'
  AND strike            = '255'
  AND option_right      = 'CE'
  AND strategy_id       = '__UNKNOWN__';

COMMIT;
