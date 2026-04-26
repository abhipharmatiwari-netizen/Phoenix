-- Core control-plane tables required by postgres_control_plane.py and schema_guard.
--
-- Previously these tables were expected to pre-exist (created via admin UI or manual
-- SQL) and db-preflight verified them without creating them, making fresh deployments
-- fail. This migration creates all five control-plane tables if they do not exist.
--
-- Column sets are derived directly from the INSERT/SELECT queries in
-- app/tenants/postgres_control_plane.py and app/core/angel_login.py.

-- Tenant registry
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   TEXT        PRIMARY KEY,
    name        TEXT        NOT NULL DEFAULT '',
    email       TEXT        NOT NULL DEFAULT '',
    phone       TEXT        NOT NULL DEFAULT '',
    status      TEXT        NOT NULL DEFAULT 'active',
    notes       TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Broker account registry
CREATE TABLE IF NOT EXISTS broker_accounts (
    broker_account_id  TEXT        PRIMARY KEY,
    tenant_id          TEXT        NOT NULL,
    broker_type        TEXT        NOT NULL DEFAULT 'angel',
    display_name       TEXT        NOT NULL DEFAULT '',
    client_code        TEXT        NOT NULL DEFAULT '',
    secret_ref         TEXT        NOT NULL DEFAULT '',
    trading_mode       TEXT        NOT NULL DEFAULT 'LIVE',
    enabled            BOOLEAN     NOT NULL DEFAULT TRUE,
    default_strategies JSONB       NOT NULL DEFAULT '[]',
    meta               JSONB       NOT NULL DEFAULT '{}',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_broker_accounts_client_code
    ON broker_accounts (client_code);

-- Subscription registry (maps tenant+broker to trading mode + active window)
CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id    TEXT        PRIMARY KEY,
    tenant_id          TEXT        NOT NULL,
    broker_account_id  TEXT        NOT NULL,
    mode               TEXT        NOT NULL DEFAULT 'LIVE',
    start_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    end_at             TIMESTAMPTZ NOT NULL DEFAULT '2099-12-31 23:59:59+00',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant_account_mode
    ON subscriptions (tenant_id, broker_account_id, mode);

-- Strategy configuration registry
CREATE TABLE IF NOT EXISTS strategy_configs (
    strategy_config_id TEXT        PRIMARY KEY,
    tenant_id          TEXT        NOT NULL,
    broker_account_id  TEXT        NOT NULL,
    strategy_id        TEXT        NOT NULL,
    enabled            BOOLEAN     NOT NULL DEFAULT TRUE,
    params             JSONB       NOT NULL DEFAULT '{}',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Broker credentials (secrets stored in Postgres for Docker Desktop LIVE mode)
-- Columns match the SELECT in app/core/angel_login.py:235-244.
CREATE TABLE IF NOT EXISTS broker_credentials (
    broker_account_id  TEXT PRIMARY KEY,
    api_key            TEXT NOT NULL DEFAULT '',
    api_secret         TEXT NOT NULL DEFAULT '',
    client_code        TEXT NOT NULL DEFAULT '',
    pin                TEXT NOT NULL DEFAULT '',
    totp_secret        TEXT NOT NULL DEFAULT '',
    client_local_ip    TEXT NOT NULL DEFAULT '',
    client_public_ip   TEXT NOT NULL DEFAULT '',
    mac_address        TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
