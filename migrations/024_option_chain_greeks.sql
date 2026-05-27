-- Migration 024: Persist Angel optionGreek REST fields on option-chain rows.
--
-- The base Angel FULL quote endpoint does not reliably include IV. Angel's
-- optionGreek REST endpoint returns implied volatility plus delta/gamma/theta/
-- vega keyed by expiry, strike, and option type. These columns are nullable so
-- base quote ingestion remains fail-soft when the enrichment endpoint is
-- unavailable.

ALTER TABLE public.option_chain_1m
    ADD COLUMN IF NOT EXISTS delta NUMERIC(18, 10),
    ADD COLUMN IF NOT EXISTS gamma NUMERIC(18, 10),
    ADD COLUMN IF NOT EXISTS theta NUMERIC(18, 10),
    ADD COLUMN IF NOT EXISTS vega  NUMERIC(18, 10);

COMMENT ON COLUMN public.option_chain_1m.iv IS
    'Implied volatility from provider quote or provider optionGreek enrichment.';

COMMENT ON COLUMN public.option_chain_1m.delta IS
    'Option delta from provider optionGreek enrichment when available.';

COMMENT ON COLUMN public.option_chain_1m.gamma IS
    'Option gamma from provider optionGreek enrichment when available.';

COMMENT ON COLUMN public.option_chain_1m.theta IS
    'Option theta from provider optionGreek enrichment when available.';

COMMENT ON COLUMN public.option_chain_1m.vega IS
    'Option vega from provider optionGreek enrichment when available.';
