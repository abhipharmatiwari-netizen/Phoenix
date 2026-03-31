-- Sprint 3 durable order submission outbox.
-- Exactly-once order submission state keyed by tenant/account/strategy/idempotency scope.

CREATE TABLE IF NOT EXISTS order_submission_outbox (
    tenant_id TEXT NOT NULL,
    broker_account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    submission_key TEXT NOT NULL,
    hub_order_id TEXT NOT NULL,
    status TEXT NOT NULL,
    order_request_json JSONB NOT NULL,
    broker_response_json JSONB,
    broker_order_id TEXT,
    submit_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    recovery_action TEXT,
    contract_key_json JSONB,
    ownership_strategy_id TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, broker_account_id, strategy_id, submission_key)
);

CREATE INDEX IF NOT EXISTS idx_order_submission_outbox_status
    ON order_submission_outbox (status, updated_at);

CREATE INDEX IF NOT EXISTS idx_order_submission_outbox_broker_order
    ON order_submission_outbox (broker_account_id, broker_order_id);
