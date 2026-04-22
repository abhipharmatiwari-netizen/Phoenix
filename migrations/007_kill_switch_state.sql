-- Migration 007: Kill switch state persistence
--
-- Creates a durable backing table for KillSwitchManager so that a TRIPPED or
-- CLEAR_PENDING kill switch survives process restarts.  Without this table,
-- kill switch state is lost on every container restart.
--
-- ARCHITECTURE s.2: kill switch listed as "authoritative runtime risk state +
-- persisted snapshot".  This migration makes the persisted snapshot real.

CREATE TABLE IF NOT EXISTS kill_switch_state (
    id              TEXT        NOT NULL,
    scope           TEXT        NOT NULL,
    scope_id        TEXT        NOT NULL,
    state           TEXT        NOT NULL DEFAULT 'INACTIVE',
    tripped_at      TEXT,
    tripped_by      TEXT,
    trip_reason     TEXT,
    cleared_at      TEXT,
    cleared_by      TEXT,
    clear_reason    TEXT,
    clear_request_id TEXT,
    updated_at      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT kill_switch_state_pk PRIMARY KEY (scope, scope_id)
);

CREATE INDEX IF NOT EXISTS idx_kill_switch_state_active
    ON kill_switch_state (state)
    WHERE state != 'INACTIVE';

COMMENT ON TABLE kill_switch_state IS
    'Durable store for KillSwitchManager records. Allows kill switch state '
    'to survive container restarts. See app/risk/kill_switch.py.';
