-- Position ownership ledger for hub-authoritative LIVE.
-- Tracks which strategy owns each option position per tenant/account.
-- Previously created lazily by runtime code; now under deterministic migration control.

CREATE TABLE IF NOT EXISTS position_ownership_ledger (
    tenant_id TEXT NOT NULL,
    broker_account_id TEXT NOT NULL,
    underlying TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike TEXT NOT NULL,
    option_right TEXT NOT NULL,
    product_type TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    authority_path TEXT NOT NULL DEFAULT 'hub',
    net_qty BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (
        tenant_id,
        broker_account_id,
        underlying,
        expiry,
        strike,
        option_right,
        product_type,
        strategy_id
    )
);

CREATE INDEX IF NOT EXISTS position_ownership_ledger_acct_idx
    ON position_ownership_ledger (tenant_id, broker_account_id);
