# Update Broker Credentials in PostgreSQL

**Applies to:** the current OCI VM deployment when it uses Postgres
`broker_credentials`.

Current production database access is through the VM-local
`phoenix-oci-postgres` container. Docker Desktop examples in old revisions are
not current production guidance.

Use this runbook when a SmartAPI credential changes for an existing `broker_account_id`.

---

## Purpose

Rotate broker login material in Postgres without moving platform secrets into repo env files.

## Scope

This runbook applies only to deployments using `BROKER_SECRET_BACKEND=postgres`.
In the Tenants UI, the broker-account `Credential Ref` field is a lookup key for
stored broker credentials only. Do not enter PIN, password, TOTP secret, API
secret, or credential values into that field.

## Preconditions

- You can connect to the Phoenix control-plane Postgres database.
- You know the target `broker_account_id`.
- You have the replacement SmartAPI values from the approved operator secrets store.
- You have a rollback copy of the existing row before making changes.

---

## What this runbook changes

This runbook updates only the broker login material stored in the `broker_credentials` table.

It does **not** update:

- `ADMIN_API_KEY`
- `DEMO_AUTH_TOKEN_SECRET`
- `CONTROL_PLANE_PG_PASSWORD`

Those values belong to the platform secret path, not the broker credential table.

---

## Table purpose

| Table | Purpose |
|---|---|
| `broker_accounts` | broker-account metadata and mapping |
| `broker_credentials` | broker login material used by Phoenix at startup / login time |

The `broker_credentials` row is selected by `broker_account_id`.

---

## Step 1 - Connect to PostgreSQL

### Option A - current OCI VM `psql`

Run on the OCI VM:

```bash
docker exec -it phoenix-oci-postgres psql -U phoenix_app -d phoenix
```

Do not print credential values into terminal logs or tickets.

### Option B - pgAdmin

1. Open pgAdmin.
2. Connect to the `phoenix` database.
3. Open Query Tool.
4. Run the SQL statements from the steps below.

### Option C - non-current local `psql` examples

These examples are retained only for local engineering and are not the current
OCI VM path:

```bash
psql -h localhost -p 5432 -U phoenix_app -d phoenix
```

Docker/host bridge example:

```bash
psql -h host.docker.internal -p 5432 -U phoenix_app -d phoenix
```

---

## Step 2 - Check whether the broker row already exists

```sql
SELECT
    broker_account_id,
    api_key IS NOT NULL AS has_api_key,
    client_code IS NOT NULL AS has_client_code,
    totp_secret IS NOT NULL AS has_totp_secret,
    updated_at
FROM broker_credentials
WHERE broker_account_id = 'A1';
```

- If one row is returned, use an `UPDATE` statement.
- If no row is returned, use the `INSERT` statement.

---

## Step 3 - Insert the broker row if it does not exist

```sql
INSERT INTO broker_credentials (
    broker_account_id,
    api_key,
    api_secret,
    client_code,
    pin,
    totp_secret,
    client_local_ip,
    client_public_ip,
    mac_address,
    updated_at
) VALUES (
    'A1',
    '<BROKER_API_KEY>',
    '',
    '<BROKER_CLIENT_CODE>',
    '<BROKER_PIN>',
    '<BROKER_TOTP_SECRET_BASE32>',
    '<BROKER_CLIENT_LOCAL_IP>',
    '<BROKER_CLIENT_PUBLIC_IP>',
    '<BROKER_MAC_ADDRESS>',
    NOW()
);
```

---

## Step 4 - Update the row when credentials rotate

### Update only the API key

```sql
UPDATE broker_credentials
SET
    api_key    = '<BROKER_API_KEY>',
    updated_at = NOW()
WHERE broker_account_id = 'A1';
```

### Update API key and public IP together

```sql
UPDATE broker_credentials
SET
    api_key          = '<BROKER_API_KEY>',
    client_public_ip = '<BROKER_CLIENT_PUBLIC_IP>',
    updated_at       = NOW()
WHERE broker_account_id = 'A1';
```

### Rotate TOTP secret

```sql
UPDATE broker_credentials
SET
    totp_secret = '<BROKER_TOTP_SECRET_BASE32>',
    updated_at  = NOW()
WHERE broker_account_id = 'A1';
```

### Full broker-credential refresh

```sql
UPDATE broker_credentials
SET
    api_key          = '<BROKER_API_KEY>',
    client_code      = '<BROKER_CLIENT_CODE>',
    pin              = '<BROKER_PIN>',
    totp_secret      = '<BROKER_TOTP_SECRET_BASE32>',
    client_local_ip  = '<BROKER_CLIENT_LOCAL_IP>',
    client_public_ip = '<BROKER_CLIENT_PUBLIC_IP>',
    mac_address      = '<BROKER_MAC_ADDRESS>',
    updated_at       = NOW()
WHERE broker_account_id = 'A1';
```

---

## Step 5 - Verify the final row

```sql
SELECT
    broker_account_id,
    api_key IS NOT NULL AS has_api_key,
    client_code IS NOT NULL AS has_client_code,
    totp_secret IS NOT NULL AS has_totp_secret,
    updated_at
FROM broker_credentials
WHERE broker_account_id = 'A1';
```

Confirm that `updated_at` reflects the change you just made. Do not display
client code, public IP, PIN, TOTP secret, or API key fragments in shared output.

Before changing a row, capture a rollback copy in the approved operator secret
store. Do not paste it into tickets or docs. For terminal evidence, capture only
non-secret presence fields:

```sql
SELECT
    broker_account_id,
    api_key IS NOT NULL AS has_api_key,
    client_code IS NOT NULL AS has_client_code,
    totp_secret IS NOT NULL AS has_totp_secret,
    updated_at
FROM broker_credentials
WHERE broker_account_id = 'A1';
```

---

## Step 6 - Restart Phoenix

Phoenix does not treat the database update as live hot-reload proof. Restart the backend after changing broker credentials.

Current OCI VM backend restart, after operator approval:

```bash
cd /opt/phoenix/app

CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-deps backend
```

The hardened watchdog no longer stops or starts nginx. Recreate nginx only if
the deployment change explicitly requires it, or if evidence shows the web
container is unhealthy after backend readiness is restored. If watchdog logs
show nginx stop/start actions, treat that as stale VM wiring and follow the OCI
runtime hardening runbook.

```bash
CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-deps nginx
```

## Validation

After restart:

- backend-local `/readyz` returns 200
- public `/readyz` and `/health/summary` remain redacted
- authenticated `/admin/health/summary` reports expected schema, watchdog, and
  tracked-account details for the logged-in operator view
- backend logs show broker login success
- no repeated `BROKER_SECRET_BACKEND=postgres` credential errors appear
- `balance_sync_ready=true` after the account runner completes its first balance sync

## Rollback / recovery

If login fails after rotation, restore the saved prior row, restart the backend, and capture the failed credential error plus the rollback SQL in the deployment record. Do not switch to env-file broker secrets in LIVE.

---

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `UPDATE 0` | wrong `broker_account_id` | run the Step 2 `SELECT` again |
| login still fails | wrong API key or client code | verify values against the SmartAPI portal |
| `Invalid TOTP` | wrong TOTP secret or local clock drift | re-copy the Base32 secret and verify server time sync |
| `IP not whitelisted` (AG7002) | `client_public_ip` does not match SmartAPI configuration | update the portal and DB row through the approved secret process; if you verify public egress from the VM/container, redact the IP in notes |
| Phoenix still uses old values | backend not restarted | restart the backend |

---

## Related

- [OCI LIVE Deployment](oci_live_deployment.md)
- [Blue/Green cutover](blue_green_cutover.md)
- `ARCHITECTURE.md`
