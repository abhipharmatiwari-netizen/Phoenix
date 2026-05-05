-- Migration 014: Add state column to broker_credentials
-- Stores broker session state (e.g. refresh tokens, last-login metadata).
ALTER TABLE broker_credentials
    ADD COLUMN IF NOT EXISTS state JSONB NOT NULL DEFAULT '{}';

INSERT INTO schema_migrations (version, applied_at)
VALUES ('014_broker_credentials_state', NOW())
ON CONFLICT DO NOTHING;
