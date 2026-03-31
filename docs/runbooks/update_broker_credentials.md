# Update Broker Credentials in PostgreSQL

**Applies to:** any Phoenix LIVE deployment that uses Postgres `broker_credentials`, including the bundled Docker/Desktop manifest in this aligned set.

In the bundled Docker/Desktop path:

- broker credentials come from Postgres `broker_credentials`
- platform/operator secrets are supplied through a separate runtime secret process
- repo env files are not broker secret sources

Use this runbook when a SmartAPI credential changes for an existing `broker_account_id`.

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

## Step 1 — Connect to PostgreSQL

### Option A — pgAdmin

1. Open pgAdmin.
2. Connect to the `phoenix` database.
3. Open Query Tool.
4. Run the SQL statements from the steps below.

### Option B — `psql`

Local database example:

```bash
psql -h localhost -p 5432 -U phoenix_app -d phoenix
```

Docker/host bridge example:

```bash
psql -h host.docker.internal -p 5432 -U phoenix_app -d phoenix
```

---

## Step 2 — Check whether the broker row already exists

```sql
SELECT
    broker_account_id,
    api_key,
    client_code,
    client_public_ip,
    updated_at
FROM broker_credentials
WHERE broker_account_id = 'A1';
```

- If one row is returned, use an `UPDATE` statement.
- If no row is returned, use the `INSERT` statement.

---

## Step 3 — Insert the broker row if it does not exist

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
    'YOUR_API_KEY',
    '',
    'YOUR_CLIENT_CODE',
    'YOUR_4_DIGIT_PIN',
    'YOUR_TOTP_SECRET_BASE32',
    '223.181.60.56',
    '223.181.60.56',
    '00-15-5D-1E-B5-C0',
    NOW()
);
```

---

## Step 4 — Update the row when credentials rotate

### Update only the API key

```sql
UPDATE broker_credentials
SET
    api_key    = 'YOUR_NEW_API_KEY',
    updated_at = NOW()
WHERE broker_account_id = 'A1';
```

### Update API key and public IP together

```sql
UPDATE broker_credentials
SET
    api_key          = 'YOUR_NEW_API_KEY',
    client_public_ip = '223.181.60.56',
    updated_at       = NOW()
WHERE broker_account_id = 'A1';
```

### Rotate TOTP secret

```sql
UPDATE broker_credentials
SET
    totp_secret = 'YOUR_NEW_TOTP_SECRET_BASE32',
    updated_at  = NOW()
WHERE broker_account_id = 'A1';
```

### Full broker-credential refresh

```sql
UPDATE broker_credentials
SET
    api_key          = 'YOUR_NEW_API_KEY',
    client_code      = 'YOUR_CLIENT_CODE',
    pin              = 'YOUR_4_DIGIT_PIN',
    totp_secret      = 'YOUR_TOTP_SECRET_BASE32',
    client_local_ip  = '223.181.60.56',
    client_public_ip = '223.181.60.56',
    mac_address      = '00-15-5D-1E-B5-C0',
    updated_at       = NOW()
WHERE broker_account_id = 'A1';
```

---

## Step 5 — Verify the final row

```sql
SELECT
    broker_account_id,
    LEFT(api_key, 6) || '****' AS api_key_masked,
    client_code,
    client_public_ip,
    updated_at
FROM broker_credentials
WHERE broker_account_id = 'A1';
```

Confirm that `updated_at` reflects the change you just made.

---

## Step 6 — Restart Phoenix

Phoenix does not treat the database update as live hot-reload proof. Restart the backend after changing broker credentials.

Bundled Docker/Desktop restart command:

```powershell
docker compose -f .\docker-compose.live.single.yml restart backend
```

If you changed multiple control-plane inputs and want a clean restart of the whole stack:

```powershell
docker compose -f .\docker-compose.live.single.yml up -d --build --force-recreate
```

---

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `UPDATE 0` | wrong `broker_account_id` | run the Step 2 `SELECT` again |
| login still fails | wrong API key or client code | verify values against the SmartAPI portal |
| `Invalid TOTP` | wrong TOTP secret or local clock drift | re-copy the Base32 secret and verify server time sync |
| `IP not whitelisted` (AG7002) | `client_public_ip` does not match SmartAPI configuration | update the portal and the DB row so they match; note the ISP (Alphion PPPoE) applies CGNAT — the visible public IP (`223.181.60.56`) differs from the router WAN IP (`100.14.130.20`); always verify with `curl https://api.ipify.org` from inside the container |
| Phoenix still uses old values | backend not restarted | restart the backend |

---

## Related

- [Docker Desktop LIVE Deployment](docker_desktop_live_deployment.md)
- [Blue/Green cutover](blue_green_cutover.md)
- `ARCHITECTURE.md`
