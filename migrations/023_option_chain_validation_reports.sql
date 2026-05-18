-- Migration 023: Cross-provider option-chain validation reports.
--
-- This table stores validation-only Angel-vs-reference data quality reports for
-- OI/ML shadow ingestion. It is not a signal table and must not be used as an
-- order queue. The full JSON payload is retained for end-of-day review.

CREATE TABLE IF NOT EXISTS public.option_chain_validation_reports (
    id                        BIGSERIAL PRIMARY KEY,
    validation_ts             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    snapshot_ts               TIMESTAMPTZ,
    underlying                TEXT        NOT NULL,
    expiry                    DATE        NOT NULL,
    primary_provider          TEXT        NOT NULL,
    reference_provider        TEXT        NOT NULL,
    status                    TEXT        NOT NULL CHECK (status IN ('OK', 'MISMATCH', 'ERROR')),
    severity                  TEXT        NOT NULL CHECK (severity IN ('INFO', 'WARN', 'ERROR')),
    compared_contracts        INTEGER     NOT NULL DEFAULT 0,
    primary_quote_count       INTEGER     NOT NULL DEFAULT 0,
    reference_quote_count     INTEGER     NOT NULL DEFAULT 0,
    mismatch_count            INTEGER     NOT NULL DEFAULT 0,
    primary_only_count        INTEGER     NOT NULL DEFAULT 0,
    reference_only_count      INTEGER     NOT NULL DEFAULT 0,
    missing_primary_iv        INTEGER     NOT NULL DEFAULT 0,
    missing_reference_iv      INTEGER     NOT NULL DEFAULT 0,
    report_payload            JSONB       NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_option_chain_validation_reports_time
    ON public.option_chain_validation_reports (validation_ts DESC);

CREATE INDEX IF NOT EXISTS idx_option_chain_validation_reports_underlying_time
    ON public.option_chain_validation_reports (underlying, expiry, validation_ts DESC);

CREATE INDEX IF NOT EXISTS idx_option_chain_validation_reports_status
    ON public.option_chain_validation_reports (status, severity, validation_ts DESC);

CREATE INDEX IF NOT EXISTS idx_option_chain_validation_reports_payload
    ON public.option_chain_validation_reports USING GIN (report_payload);

COMMENT ON TABLE public.option_chain_validation_reports IS
    'Validation-only cross-provider option-chain reports for OI/ML shadow ingestion. '
    'Rows are for data-quality review only and must not be routed as trading signals.';

COMMENT ON COLUMN public.option_chain_validation_reports.report_payload IS
    'Full JSON validation report including per-contract field differences, provider-only '
    'contracts, and operational metadata for end-of-day review.';
