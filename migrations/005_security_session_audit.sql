-- Migration 005: Security (session revocation, refresh tokens) + immutable audit store
-- PHX-SEC-005, PHX-AUD-001

-- Revoked access token JTIs (PHX-SEC-005)
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti         TEXT        NOT NULL,
    revoked_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (jti)
);
CREATE INDEX IF NOT EXISTS idx_revoked_tokens_exp ON revoked_tokens (expires_at);

-- Refresh tokens (PHX-SEC-005)
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token       TEXT        NOT NULL,
    user_id     TEXT        NOT NULL,
    email       TEXT        NOT NULL,
    role        TEXT        NOT NULL DEFAULT 'viewer',
    issued_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at  TIMESTAMPTZ NOT NULL,
    used        BOOLEAN     NOT NULL DEFAULT FALSE,
    used_at     TIMESTAMPTZ,
    PRIMARY KEY (token)
);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens (user_id, used, expires_at);

-- Immutable audit events table (PHX-AUD-001)
-- Rows are INSERT-only; UPDATE/DELETE are not permitted by application code.
CREATE TABLE IF NOT EXISTS audit_events (
    audit_id        TEXT        NOT NULL,
    timestamp       TEXT        NOT NULL,
    actor           TEXT        NOT NULL,
    action          TEXT        NOT NULL,
    resource_type   TEXT        NOT NULL,
    resource_id     TEXT        NOT NULL,
    request_id      TEXT,
    idempotency_key TEXT,
    before_state    JSONB,
    after_state     JSONB,
    metadata        JSONB,
    PRIMARY KEY (audit_id)
);
CREATE INDEX IF NOT EXISTS idx_audit_events_actor    ON audit_events (actor, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_action   ON audit_events (action, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_resource ON audit_events (resource_type, resource_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_ts       ON audit_events (timestamp DESC);

-- Deny UPDATE and DELETE on audit_events (enforce append-only via role)
-- Note: in Postgres, apply this as a row-level security policy or via a trigger.
-- The application code never issues UPDATE/DELETE on this table.
