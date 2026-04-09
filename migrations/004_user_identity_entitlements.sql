-- P0 security hardening: bind user identity to tenant/account entitlements.

CREATE TABLE IF NOT EXISTS user_tenant_entitlements (
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT,
    PRIMARY KEY (user_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_user_tenant_entitlements_tenant
    ON user_tenant_entitlements (tenant_id, user_id);

CREATE TABLE IF NOT EXISTS user_broker_account_entitlements (
    user_id TEXT NOT NULL,
    broker_account_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT,
    PRIMARY KEY (user_id, broker_account_id)
);

CREATE INDEX IF NOT EXISTS idx_user_broker_account_entitlements_account
    ON user_broker_account_entitlements (broker_account_id, user_id);
