-- Migration 024: terminal position records must not carry open net quantity.
--
-- Prior force-clear paths could mark internal_position_records as FLAT while
-- leaving net_qty nonzero. Readiness/load paths ignore terminal rows, so this
-- migration normalizes the durable terminal quantity and adds a constraint to
-- prevent recurrence.

DO $do$
BEGIN
    IF to_regclass('public.internal_position_records') IS NOT NULL THEN
        IF to_regclass('public.audit_events') IS NOT NULL THEN
            INSERT INTO public.audit_events (
                audit_id,
                timestamp,
                actor,
                action,
                resource_type,
                resource_id,
                before_state,
                after_state,
                metadata
            )
            SELECT
                'migration_024_terminal_net_qty_' || md5(scope_key),
                NOW()::TEXT,
                'migration_024',
                'terminal_position_record_net_qty_zeroed',
                'internal_position_record',
                scope_key,
                jsonb_build_object(
                    'position_state', position_state,
                    'net_qty', net_qty,
                    'unrealized_pnl', unrealized_pnl,
                    'state_reason', state_reason
                ),
                jsonb_build_object(
                    'position_state', position_state,
                    'net_qty', 0,
                    'unrealized_pnl', 0,
                    'state_reason_suffix', 'migration_024_terminal_net_qty_zeroed'
                ),
                jsonb_build_object(
                    'migration', '024_terminal_position_record_invariant.sql',
                    'invariant', 'terminal FLAT/NONE records must have net_qty=0'
                )
            FROM public.internal_position_records
            WHERE position_state IN ('FLAT', 'NONE')
              AND ABS(COALESCE(net_qty, 0)) > 0.0001
            ON CONFLICT (audit_id) DO NOTHING;
        END IF;

        UPDATE public.internal_position_records
        SET net_qty = 0,
            unrealized_pnl = 0,
            state_reason = CASE
                WHEN COALESCE(state_reason, '') = ''
                    THEN 'migration_024_terminal_net_qty_zeroed'
                WHEN state_reason LIKE '%migration_024_terminal_net_qty_zeroed%'
                    THEN state_reason
                ELSE state_reason || '; migration_024_terminal_net_qty_zeroed'
            END,
            updated_at = NOW()
        WHERE position_state IN ('FLAT', 'NONE')
          AND ABS(COALESCE(net_qty, 0)) > 0.0001;

        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'public.internal_position_records'::regclass
              AND conname = 'chk_internal_position_records_terminal_net_qty_zero'
        ) THEN
            ALTER TABLE public.internal_position_records
                ADD CONSTRAINT chk_internal_position_records_terminal_net_qty_zero
                CHECK (
                    position_state NOT IN ('FLAT', 'NONE')
                    OR ABS(COALESCE(net_qty, 0)) <= 0.0001
                );
        END IF;
    END IF;
END
$do$;
