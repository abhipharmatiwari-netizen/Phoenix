-- Migration 014: Add state column to broker_credentials
-- Stores broker session state (e.g. refresh tokens, last-login metadata).
ALTER TABLE broker_credentials
    ADD COLUMN IF NOT EXISTS state JSONB NOT NULL DEFAULT '{}';
