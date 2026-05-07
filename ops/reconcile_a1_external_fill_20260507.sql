-- One-shot reconciliation: pnl_snapshots was missing one fill — the manual
-- MARKET SELL @ 17.30 placed via Angel UI directly at 21:47:59 IST today.
--
-- Phoenix's _emit_trade only fires for orders Phoenix placed itself, so the
-- 1250-unit credit (+₹21,625) from that external fill never reached the
-- cash-flow ledger. Without it:
--   Phoenix realized = -17,875 (3 of 4 fills booked)
--   Angel realized   =  +3,750 (all 4 fills, ground truth)
--   Delta            = +21,625 = 1250 × 17.30
--
-- This patch sets ema20_strategy.realized_pnl directly to the broker truth
-- so the dashboard reads correctly tonight. Subsequent fills are handled by
-- the new ExternalFillReconciler shipped alongside this commit, which makes
-- this manual patch unnecessary going forward.
--
-- Procedure: docker stop phoenix-oci-backend FIRST, then run this, then start.
-- Without stopping, the running backend will save the in-memory -17,875 back
-- to Postgres on shutdown and revert this patch.

BEGIN;

UPDATE pnl_snapshots
SET realized_pnl     = 3750.0,
    as_of            = NOW(),
    freshness_source = 'manual_reconcile_post_external_fill_20260507',
    updated_at       = NOW()
WHERE tenant_id         = 'tenant-1'
  AND broker_account_id = 'A1'
  AND strategy_id       = 'ema20_strategy';

COMMIT;
