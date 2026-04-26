-- Sprint 9: Single-Name Strategy Harmonization
-- Migrate all strategy_id references from kebab-case to canonical snake_case names.
-- Underlying information moved to separate metadata in strategy configurations.

-- Create legacy-to-canonical strategy name mapping
-- This table tracks the migration and can be used for audit/rollback if needed
CREATE TABLE IF NOT EXISTS strategy_migration_log (
    legacy_strategy_id TEXT NOT NULL,
    canonical_strategy_name TEXT NOT NULL,
    migration_timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    affected_table TEXT NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (legacy_strategy_id, canonical_strategy_name, affected_table)
);

-- Migration mapping: legacy kebab-case IDs to canonical snake_case names
-- This is a one-time mapping used during the migration
CREATE TEMP TABLE legacy_strategy_mapping AS
SELECT 'ng-options'::TEXT AS legacy_id, 'option_strategy'::TEXT AS canonical_name
UNION ALL SELECT 'nifty-options', 'option_strategy'
UNION ALL SELECT 'banknifty-options', 'option_strategy'
UNION ALL SELECT 'finnifty-options', 'option_strategy'
UNION ALL SELECT 'sensex-options', 'option_strategy'
UNION ALL SELECT 'midcpnifty-options', 'option_strategy'
UNION ALL SELECT 'nifty-delta-strangle', 'delta_strangle'
UNION ALL SELECT 'banknifty-delta-strangle', 'delta_strangle'
UNION ALL SELECT 'ema20-trend', 'ema20_strategy'
UNION ALL SELECT 'ce-orb', 'ce_orb'
UNION ALL SELECT 'ce-pdh-rb', 'ce_pdh_rb'
UNION ALL SELECT 'put-momentum-scalper', 'put_momentum_scalper'
UNION ALL SELECT 'put-reversion-writer', 'put_reversion_writer'
UNION ALL SELECT 'nifty-weekly-credit-spreads', 'nifty_weekly_credit_spreads'
UNION ALL SELECT 'exclusive-nifty-ce-buy', 'exclusive_nifty_ce_buy'
UNION ALL SELECT 'put-buy', 'put_buy';

-- Rewrite canonical strategy identifiers in live-readable Postgres tables when present.
-- Every block is idempotent: rerunning after cutover performs no-op updates.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'strategy_configs'
          AND column_name = 'strategy_id'
    ) THEN
        WITH updated AS (
            UPDATE public.strategy_configs AS sc
            SET strategy_id = map.canonical_name
            FROM legacy_strategy_mapping AS map
            WHERE sc.strategy_id = map.legacy_id
            RETURNING map.legacy_id, map.canonical_name
        )
        INSERT INTO strategy_migration_log (
            legacy_strategy_id,
            canonical_strategy_name,
            affected_table,
            record_count
        )
        SELECT
            legacy_id,
            canonical_name,
            'strategy_configs.strategy_id'::TEXT,
            COUNT(*)
        FROM updated
        GROUP BY legacy_id, canonical_name
        ON CONFLICT (legacy_strategy_id, canonical_strategy_name, affected_table)
        DO UPDATE SET
            record_count = GREATEST(
                strategy_migration_log.record_count,
                EXCLUDED.record_count
            ),
            migration_timestamp = CURRENT_TIMESTAMP;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'order_submission_outbox'
          AND column_name = 'strategy_id'
    ) THEN
        WITH updated AS (
            UPDATE public.order_submission_outbox AS outbox
            SET strategy_id = map.canonical_name
            FROM legacy_strategy_mapping AS map
            WHERE outbox.strategy_id = map.legacy_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.order_submission_outbox AS existing
                  WHERE existing.tenant_id = outbox.tenant_id
                    AND existing.broker_account_id = outbox.broker_account_id
                    AND existing.strategy_id = map.canonical_name
                    AND existing.submission_key = outbox.submission_key
              )
            RETURNING map.legacy_id, map.canonical_name
        )
        INSERT INTO strategy_migration_log (
            legacy_strategy_id,
            canonical_strategy_name,
            affected_table,
            record_count
        )
        SELECT
            legacy_id,
            canonical_name,
            'order_submission_outbox.strategy_id'::TEXT,
            COUNT(*)
        FROM updated
        GROUP BY legacy_id, canonical_name
        ON CONFLICT (legacy_strategy_id, canonical_strategy_name, affected_table)
        DO UPDATE SET
            record_count = GREATEST(
                strategy_migration_log.record_count,
                EXCLUDED.record_count
            ),
            migration_timestamp = CURRENT_TIMESTAMP;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'order_submission_outbox'
          AND column_name = 'ownership_strategy_id'
    ) THEN
        WITH updated AS (
            UPDATE public.order_submission_outbox AS outbox
            SET ownership_strategy_id = map.canonical_name
            FROM legacy_strategy_mapping AS map
            WHERE outbox.ownership_strategy_id = map.legacy_id
            RETURNING map.legacy_id, map.canonical_name
        )
        INSERT INTO strategy_migration_log (
            legacy_strategy_id,
            canonical_strategy_name,
            affected_table,
            record_count
        )
        SELECT
            legacy_id,
            canonical_name,
            'order_submission_outbox.ownership_strategy_id'::TEXT,
            COUNT(*)
        FROM updated
        GROUP BY legacy_id, canonical_name
        ON CONFLICT (legacy_strategy_id, canonical_strategy_name, affected_table)
        DO UPDATE SET
            record_count = GREATEST(
                strategy_migration_log.record_count,
                EXCLUDED.record_count
            ),
            migration_timestamp = CURRENT_TIMESTAMP;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'trade_processed_markers'
          AND column_name = 'strategy_id'
    ) THEN
        WITH updated AS (
            UPDATE public.trade_processed_markers AS markers
            SET strategy_id = map.canonical_name
            FROM legacy_strategy_mapping AS map
            WHERE markers.strategy_id = map.legacy_id
            RETURNING map.legacy_id, map.canonical_name
        )
        INSERT INTO strategy_migration_log (
            legacy_strategy_id,
            canonical_strategy_name,
            affected_table,
            record_count
        )
        SELECT
            legacy_id,
            canonical_name,
            'trade_processed_markers.strategy_id'::TEXT,
            COUNT(*)
        FROM updated
        GROUP BY legacy_id, canonical_name
        ON CONFLICT (legacy_strategy_id, canonical_strategy_name, affected_table)
        DO UPDATE SET
            record_count = GREATEST(
                strategy_migration_log.record_count,
                EXCLUDED.record_count
            ),
            migration_timestamp = CURRENT_TIMESTAMP;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'broker_accounts'
          AND column_name = 'default_strategies'
    ) THEN
        WITH impacted AS (
            SELECT
                map.legacy_id,
                map.canonical_name,
                COUNT(DISTINCT ba.broker_account_id) AS affected_count
            FROM public.broker_accounts AS ba
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE
                    WHEN jsonb_typeof(COALESCE(ba.default_strategies, '[]'::jsonb)) = 'array'
                    THEN COALESCE(ba.default_strategies, '[]'::jsonb)
                    ELSE '[]'::jsonb
                END
            ) AS elem(value)
            JOIN legacy_strategy_mapping AS map
              ON map.legacy_id = elem.value
            GROUP BY map.legacy_id, map.canonical_name
        ),
        expanded AS (
            SELECT
                ba.broker_account_id,
                elem.ordinality,
                COALESCE(map.canonical_name, elem.value) AS canonical_name
            FROM public.broker_accounts AS ba
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE
                    WHEN jsonb_typeof(COALESCE(ba.default_strategies, '[]'::jsonb)) = 'array'
                    THEN COALESCE(ba.default_strategies, '[]'::jsonb)
                    ELSE '[]'::jsonb
                END
            ) WITH ORDINALITY AS elem(value, ordinality)
            LEFT JOIN legacy_strategy_mapping AS map
              ON map.legacy_id = elem.value
        ),
        deduped AS (
            SELECT
                broker_account_id,
                canonical_name,
                MIN(ordinality) AS first_ordinality
            FROM expanded
            GROUP BY broker_account_id, canonical_name
        ),
        rebuilt AS (
            SELECT
                broker_account_id,
                COALESCE(
                    jsonb_agg(to_jsonb(canonical_name) ORDER BY first_ordinality),
                    '[]'::jsonb
                ) AS new_default_strategies
            FROM deduped
            GROUP BY broker_account_id
        ),
        updated_accounts AS (
            UPDATE public.broker_accounts AS ba
            SET default_strategies = rebuilt.new_default_strategies
            FROM rebuilt
            WHERE ba.broker_account_id = rebuilt.broker_account_id
              AND ba.default_strategies IS DISTINCT FROM rebuilt.new_default_strategies
            RETURNING ba.broker_account_id
        )
        INSERT INTO strategy_migration_log (
            legacy_strategy_id,
            canonical_strategy_name,
            affected_table,
            record_count
        )
        SELECT
            legacy_id,
            canonical_name,
            'broker_accounts.default_strategies'::TEXT,
            affected_count
        FROM impacted
        WHERE affected_count > 0
        ON CONFLICT (legacy_strategy_id, canonical_strategy_name, affected_table)
        DO UPDATE SET
            record_count = GREATEST(
                strategy_migration_log.record_count,
                EXCLUDED.record_count
            ),
            migration_timestamp = CURRENT_TIMESTAMP;
    END IF;
END $$;

-- Create index for faster lookups during transition period
CREATE INDEX IF NOT EXISTS idx_strategy_migration_log_legacy
    ON strategy_migration_log(legacy_strategy_id);

-- Rollback helper: This view can be used to see the mapping
CREATE OR REPLACE VIEW strategy_migration_mapping AS
SELECT 
    'ng-options'::TEXT AS legacy_id, 'option_strategy'::TEXT AS canonical_name
UNION ALL SELECT 'nifty-options', 'option_strategy'
UNION ALL SELECT 'banknifty-options', 'option_strategy'
UNION ALL SELECT 'finnifty-options', 'option_strategy'
UNION ALL SELECT 'sensex-options', 'option_strategy'
UNION ALL SELECT 'midcpnifty-options', 'option_strategy'
UNION ALL SELECT 'nifty-delta-strangle', 'delta_strangle'
UNION ALL SELECT 'banknifty-delta-strangle', 'delta_strangle'
UNION ALL SELECT 'ema20-trend', 'ema20_strategy'
UNION ALL SELECT 'ce-orb', 'ce_orb'
UNION ALL SELECT 'ce-pdh-rb', 'ce_pdh_rb'
UNION ALL SELECT 'put-momentum-scalper', 'put_momentum_scalper'
UNION ALL SELECT 'put-reversion-writer', 'put_reversion_writer'
UNION ALL SELECT 'nifty-weekly-credit-spreads', 'nifty_weekly_credit_spreads'
UNION ALL SELECT 'exclusive-nifty-ce-buy', 'exclusive_nifty_ce_buy'
UNION ALL SELECT 'put-buy', 'put_buy';

-- Canonical strategy registry (for reference/documentation)
CREATE TABLE IF NOT EXISTS canonical_strategy_registry (
    canonical_name TEXT PRIMARY KEY NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT,
    underlying_agnostic BOOLEAN NOT NULL DEFAULT true,
    added_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO canonical_strategy_registry 
(canonical_name, display_name, description, underlying_agnostic) 
VALUES 
('option_strategy', 'Option Strategy', 'Multi-underlying options trading strategy', true),
('delta_strangle', 'Delta Strangle', 'Delta-neutral strangle strategy', true),
('ema20_strategy', 'EMA 20 Strategy', 'Exponential Moving Average (20-period) trend following', true),
('ce_orb', 'CE ORB', 'Call Entry - Open Range Breakout', false),
('ce_pdh_rb', 'CE PDH RB', 'Call Entry - Previous Day High Range Breakout', false),
('put_momentum_scalper', 'Put Momentum Scalper', 'Put option momentum scalping strategy', true),
('put_reversion_writer', 'Put Reversion Writer', 'Put reversion and writing strategy', true),
('nifty_weekly_credit_spreads', 'Nifty Weekly Credit Spreads', 'Weekly credit spread strategy on NIFTY', false),
('exclusive_nifty_ce_buy', 'Exclusive Nifty CE Buy', 'NIFTY-exclusive call entry buy strategy', false),
('put_buy', 'Put Buy', 'Put option buying strategy', true)
ON CONFLICT DO NOTHING;

-- Create index for strategy registry lookups
CREATE INDEX IF NOT EXISTS idx_canonical_strategy_registry_name
    ON canonical_strategy_registry(canonical_name);
