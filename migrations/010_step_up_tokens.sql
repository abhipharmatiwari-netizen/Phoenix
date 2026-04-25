-- Migration 010: Step-up authorization tokens (issue #110)
--
-- Persists step-up tokens to Postgres so pending maker-checker approvals survive
-- a container restart. In-memory cache is the fast path; this table is the
-- authoritative durable store loaded on startup.
--
-- Only unexpired, unused tokens are loaded on startup (expires_at > NOW(), used = FALSE).

CREATE TABLE IF NOT EXISTS step_up_tokens (
    token_id        TEXT PRIMARY KEY,
    actor           TEXT NOT NULL,
    action_class    TEXT NOT NULL,
    resource_id     TEXT,
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    used            BOOLEAN NOT NULL DEFAULT FALSE,
    used_at         TIMESTAMPTZ,
    approver        TEXT,
    approved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_step_up_tokens_expires
    ON step_up_tokens (expires_at)
    WHERE used = FALSE;

-- Automatic cleanup: tokens expired for more than 24 hours are deleted on the next
-- startup or a periodic maintenance job. This prevents unbounded table growth.
-- Application code also prunes on startup via DELETE WHERE expires_at < NOW() - INTERVAL '24 hours'.

COMMENT ON TABLE step_up_tokens IS
    'Durable store for step-up authorization tokens (issue #110 / ARCHITECTURE §15.4). '
    'In-memory cache is the primary fast path; this table persists tokens across restarts.';
