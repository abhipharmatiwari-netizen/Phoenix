# Phoenix OCI Live Deployment

> **Status:** Active production deployment runbook for the Oracle Cloud Infrastructure (OCI) path.
> Supersedes any ad-hoc cloud notes. Covers first-time setup, normal redeploy, and troubleshooting.
>
> **Security note:** Real infrastructure IDs (OCIDs, IPs, namespaces, client codes) are stored in the
> private ops secrets store, not in this file. Use `<PLACEHOLDER>` values here and retrieve actuals
> from the operator secrets store before running any command.

---

## Architecture overview

```
Internet users / Angel One postback
        │
        ▼ HTTP :80 (redirects to HTTPS) / HTTPS :443
OCI Load Balancer — phoenix-lb (<LB_PUBLIC_IP>, public subnet)
        │ port 80 → VM:80   port 443 (TCP passthrough) → VM:8443
        ▼
OCI Mumbai — phoenix-vm (<VM_PRIVATE_IP>, private subnet)
  NAT Gateway outbound IP: <NAT_GATEWAY_IP>
        │
  docker phoenix-oci-web      (nginx, :80 redirect + :8443 TLS)
  docker phoenix-oci-backend  (image from OCIR, :8080)
  docker phoenix-oci-postgres (Postgres)
        │
        ▼ (outbound via NAT — Angel One blocks direct OCI IPs)
Vultr Mumbai VPS — phoenix-proxy (<PROXY_IP>)
  tinyproxy :8888  ◄── only accepts from <NAT_GATEWAY_IP>
        │
        ▼ (HTTPS CONNECT tunnel)
Angel One SmartAPI / WebSocket (apiconnect.angelone.in)
```

**Why the proxy?** Angel One's SmartAPI firewall blocks OCI's cloud IP ranges at the TCP level.
All Angel One HTTP (REST) and WebSocket traffic is tunnelled through the Vultr proxy via HTTP CONNECT.

**HTTPS:** TLS is terminated at nginx on the OCI VM (port 8443). The OCI LB uses TCP passthrough on
port 443 so the full TLS certificate chain reaches the client. Certificate: Let's Encrypt via
`<PHOENIX_DOMAIN>`, expires 90 days, auto-renewed.

---

## Key infrastructure IDs

> **All real values are stored in the private ops secrets store.** Replace each `<PLACEHOLDER>` below
> with the actual value from that store before executing any command.

| Resource | Placeholder |
|---|---|
| **Dashboard URL** | `https://<PHOENIX_DOMAIN>` |
| OCI VM private IP | `<VM_PRIVATE_IP>` |
| OCI NAT gateway outbound IP | `<NAT_GATEWAY_IP>` |
| OCI Load Balancer public IP | `<LB_PUBLIC_IP>` |
| Nginx HTTP port (redirect) | `80` |
| Nginx HTTPS port (TLS) | `8443` |
| TLS domain | `<PHOENIX_DOMAIN>` |
| TLS certificate | Let's Encrypt (ECDSA P-256, expires 90 days) |
| Cert renewal cron | 1st and 15th of each month, 03:00 IST |
| Vultr proxy IP | `<PROXY_IP>` |
| Proxy port | `8888` |
| OCI compartment OCID | `<COMPARTMENT_OCID>` |
| OCI instance OCID | `<INSTANCE_OCID>` |
| OCI bastion OCID | `<BASTION_OCID>` |
| OCI LB OCID | `<LB_OCID>` |
| OCIR namespace | `<OCIR_NAMESPACE>` |
| OCIR region | `ap-mumbai-1` |

---

## Angel One developer portal settings

Log into [smartapi.angelbroking.com](https://smartapi.angelbroking.com) and configure the API app:

| Field | Value |
|---|---|
| **IP Whitelist** | `<NAT_GATEWAY_IP>` (OCI NAT gateway outbound IP) |
| **Redirect URL** | `https://<PHOENIX_DOMAIN>/` |
| **Postback URL** | `https://<PHOENIX_DOMAIN>/webhook/angel/postback` |

The postback URL receives Angel One order fill events. Without it, the lifecycle service
never receives fills and positions remain unreconciled.

---

## SSH access via OCI Bastion

Bastion sessions expire. Create a new one whenever SSH access is needed.
Retrieve `<BASTION_OCID>`, `<INSTANCE_OCID>`, and `<SSH_KEY_PATH>` from the ops secrets store.

```bash
# From local Windows machine (PowerShell)
$pubKey = Get-Content "<SSH_KEY_PATH>.pub" -Raw
$pubKey.Trim() | Out-File "$env:TEMP\pub.txt" -NoNewline -Encoding ascii

$session = oci bastion session create-managed-ssh `
  --bastion-id <BASTION_OCID> `
  --target-resource-id <INSTANCE_OCID> `
  --target-os-username opc `
  --ssh-public-key-file "$env:TEMP\pub.txt" `
  --session-ttl 10800 `
  --display-name phoenix-session `
  --region ap-mumbai-1 2>&1 | ConvertFrom-Json
$SESSION_ID = $session.data.id

# Wait for ACTIVE
until (oci bastion session get --session-id $SESSION_ID --region ap-mumbai-1 2>/dev/null | grep -q '"ACTIVE"'); do sleep 5; done
```

Then SSH (key-only — password auth is disabled on the VM):

```bash
KEY="<SSH_KEY_PATH>"
BASTION_HOST="host.bastion.ap-mumbai-1.oci.oraclecloud.com"

# Add bastion and instance host keys to known_hosts before first use:
ssh-keyscan -H "${SESSION_ID}.${BASTION_HOST}" >> ~/.ssh/known_hosts
ssh-keyscan -H -p 22 -J "${SESSION_ID}@${BASTION_HOST}" <VM_PRIVATE_IP> >> ~/.ssh/known_hosts

ssh -i "$KEY" \
  -o "ProxyCommand=ssh -i \"$KEY\" -W %h:%p -p 22 ${SESSION_ID}@${BASTION_HOST}" \
  -p 22 opc@<VM_PRIVATE_IP>
```

> **Security:** `StrictHostKeyChecking` is left at its default (`ask` / `yes`). Never pass
> `-o StrictHostKeyChecking=no` — this disables MITM protection.

---

## Files on the OCI VM

All persistent deployment state lives under `/opt/phoenix/`:

```
/opt/phoenix/
├── app/                              # Git checkout of Phoenix repo
│   ├── docker-compose.oci-live.yml   # Base compose manifest (OCIR images)
│   └── nginx/nginx-ssl.conf.template # SSL nginx config template (repo-tracked)
├── phoenix-override.yml          # Compose override: env overrides (see phoenix-override.yml.example)
├── phoenix-deploy.env            # Required non-secret env vars for docker compose
├── renew-cert.sh                 # TLS cert renewal script (called by cron)
├── acme-challenge/               # Let's Encrypt HTTP-01 challenge webroot
├── certs/                        # Let's Encrypt certificates (live/ and archive/)
│   └── live/<PHOENIX_DOMAIN>/
│       ├── fullchain.pem         # Cert chain (mounted into nginx)
│       └── privkey.pem           # Private key (mounted into nginx)
├── state/                        # Persistent runtime state (risk, positions)
└── logs/                         # Application logs + cert-renewal.log
```

> **No source-file bind mounts.** All proxy and watchdog patches are tracked in the git repo
> and baked into the OCIR image at build time. The `phoenix-override.yml` must NOT bind-mount
> individual `.py` files. See `phoenix-override.yml.example` in the repo root for the approved
> override template.

---

## Deploy command (normal redeploy)

From `/opt/phoenix/app` on the OCI VM:

```bash
cd /opt/phoenix/app
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-deps backend
```

This recreates **only the backend** container. The override applies:
- `ANGEL_HTTPS_PROXY=http://<PROXY_IP>:8888` as a real container env var
- `CORS_ORIGINS=https://<PHOENIX_DOMAIN>`
- `CONTROL_PLANE_DB_DSN` pointing to the local Postgres container
- `ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY` — **must be removed** once real capital limits are set

### Redeploy nginx (after cert changes or nginx config update)

The nginx container reads `ADMIN_API_KEY` directly from `/run/secrets/admin_api_key` at start.
**Do not render or write the nginx config to the host filesystem.** Simply force-recreate the
container so the internal entrypoint re-runs envsubst:

```bash
cd /opt/phoenix/app
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-deps --force-recreate nginx
```

The container entrypoint (`/docker-entrypoint.d/90-envsubst-admin-key.sh`) reads the secret
from `/run/secrets/admin_api_key` and renders it into `/etc/nginx/conf.d/default.conf` inside
the container. The rendered config never touches the host filesystem.

### After code changes

1. Push changes to git → CI builds and pushes a new OCIR image tagged with the commit SHA.
2. Pull and deploy the new image:

```bash
cd /opt/phoenix/app
IMAGE_TAG=<GIT_SHA> docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-deps backend
```

> **Never SCP individual `.py` files onto the VM and rely on bind mounts.**
> All changes must go through git → CI → OCIR image → compose deploy.

---

## Proxy server (Vultr)

The proxy is a Vultr Mumbai VPS running tinyproxy. Connect via SSH key (password auth is disabled):

```bash
ssh -i <SSH_KEY_PATH> <PROXY_USER>@<PROXY_IP>

# Check tinyproxy status
systemctl is-active tinyproxy
ss -tlnp | grep 8888
```

Expected: `active` and `LISTEN` on port 8888.

### Config file

`/etc/tinyproxy/tinyproxy.conf`:
```
User tinyproxy
Group tinyproxy
Port 8888
Timeout 600
LogLevel Warning
MaxClients 20
Allow <NAT_GATEWAY_IP>
```

`Restart=always` is set via `/etc/systemd/system/tinyproxy.service.d/restart.conf` —
tinyproxy auto-restarts on crash and on VM reboot.

### UFW rules (proxy VM)

```bash
# Port 8888 accepts only from the OCI NAT gateway:
ufw allow from <NAT_GATEWAY_IP> to any port 8888
# SSH accepts only from the OCI NAT gateway (operator accesses via OCI bastion → proxy):
ufw allow from <NAT_GATEWAY_IP> to any port 22
ufw deny 22
ufw enable
```

---

## Database initial seed (first-time only)

If the control-plane DB is empty (fresh deployment), seed the required rows.
Retrieve `<TENANT_ID>`, `<BROKER_ACCOUNT_ID>`, `<CLIENT_CODE>`, and `<ADMIN_EMAIL>` from
the ops secrets store.

```bash
# On the OCI VM — use parameterized psql variables, never string interpolation
docker exec -i phoenix-oci-postgres psql -U phoenix_app -d phoenix << 'SQL'
INSERT INTO tenants (tenant_id, name, email, phone, status, notes)
VALUES (:'tenant_id', 'Phoenix Live', :'admin_email', '', 'active', 'Primary live trading tenant')
ON CONFLICT (tenant_id) DO NOTHING;

INSERT INTO broker_accounts
  (broker_account_id, tenant_id, broker_type, display_name, client_code, secret_ref, trading_mode, enabled)
VALUES (:'broker_account_id', :'tenant_id', 'angel', 'Angel One', :'client_code', :'broker_account_id', 'LIVE', true)
ON CONFLICT (broker_account_id) DO NOTHING;

INSERT INTO subscriptions (subscription_id, tenant_id, broker_account_id, mode, start_at, end_at)
VALUES ('sub-live', :'tenant_id', :'broker_account_id', 'LIVE', now(), '2099-12-31 23:59:59+00')
ON CONFLICT (subscription_id) DO NOTHING;

INSERT INTO strategy_configs (strategy_config_id, tenant_id, broker_account_id, strategy_id, enabled, params)
VALUES
  ('sc-ema20',       :'tenant_id', :'broker_account_id', 'ema20_strategy',        true, '{}'),
  ('sc-ce-buy',      :'tenant_id', :'broker_account_id', 'exclusive_nifty_ce_buy',true, '{}'),
  ('sc-put-scalper', :'tenant_id', :'broker_account_id', 'put_momentum_scalper',  true, '{}')
ON CONFLICT (strategy_config_id) DO NOTHING;
SQL
```

Pass variables with `psql -v tenant_id=<TENANT_ID> -v broker_account_id=<BROKER_ACCOUNT_ID> ...`.

Broker credentials (`broker_credentials` table) must already be populated via the
`update_broker_credentials.md` runbook.

---

## Admin user setup (first-time only)

```bash
# On OCI VM — uses parameterized psycopg to avoid SQL injection
python3 - << 'PYEOF'
import hashlib, secrets, base64, uuid
import psycopg, os

password = input("Admin password: ")
salt = secrets.token_hex(16)
key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
b64 = base64.urlsafe_b64encode(key).rstrip(b"=").decode()
phash = f"pbkdf2${salt}${b64}"
uid = str(uuid.uuid4())

dsn = os.environ["CONTROL_PLANE_DSN"]
admin_email = os.environ["ADMIN_EMAIL"]   # set this before running
tenant_id = os.environ["TENANT_ID"]       # set this before running

with psycopg.connect(dsn, autocommit=True) as conn:
    conn.execute(
        "INSERT INTO users (id, email, name, password_hash, role) "
        "VALUES (%s, %s, 'Admin', %s, 'admin') ON CONFLICT DO NOTHING",
        (uid, admin_email, phash),
    )
    conn.execute(
        "INSERT INTO user_tenant_entitlements (user_id, tenant_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (uid, tenant_id),
    )
print(f"Created admin user: {admin_email}")
PYEOF
```

> Set `CONTROL_PLANE_DSN`, `ADMIN_EMAIL`, and `TENANT_ID` in the shell before running.
> Login at `https://<PHOENIX_DOMAIN>/login`.

---

## Verification after deploy

### 1. Health and readiness

```bash
# From local machine — use HTTPS
curl https://<PHOENIX_DOMAIN>/health | python3 -m json.tool
curl https://<PHOENIX_DOMAIN>/readyz | python3 -m json.tool
```

Expected:
- `/health`: `"stream_worker_running": true, "watchdog_running": true`
- `/readyz`: `"ready": true`

### 2. TLS / security headers

```bash
curl -I https://<PHOENIX_DOMAIN>/health 2>&1 | grep -E 'strict-transport|x-frame|x-content|content-security'
```

Expected: `Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`, `Content-Security-Policy`.

```bash
# HTTP must redirect to HTTPS (301)
curl -I http://<PHOENIX_DOMAIN>/ 2>&1 | grep -E 'HTTP/|Location'
```

### 3. Angel One connectivity

```bash
# From OCI VM
timeout 6 bash -c 'cat < /dev/null > /dev/tcp/apiconnect.angelone.in/443' \
  && echo 'DIRECT: OPEN' || echo 'DIRECT: BLOCKED (expected — routed via proxy)'

# Confirm proxy is forwarding
docker exec phoenix-oci-backend curl -s --max-time 8 \
  -x http://<PROXY_IP>:8888 \
  https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword \
  -o /dev/null -w "%{http_code}"
# Expected: 200 or 400 (not a timeout)
```

### 4. Live tick stream

```bash
docker logs phoenix-oci-backend --tail 20 2>&1 | grep "BAR CLOSED"
```

---

## TLS certificate management

### Current certificate

| Property | Value |
|---|---|
| Domain | `<PHOENIX_DOMAIN>` |
| CA | Let's Encrypt (ECDSA P-256 / E7 intermediate) |
| Location on VM | `/opt/phoenix/certs/live/<PHOENIX_DOMAIN>/` |
| Expires | 90 days from issue; renews every ~60 days via cron |

### Auto-renewal (cron)

Runs at 03:00 IST on 1st and 15th of each month:
```bash
sudo crontab -l | grep renew
```

The renewal script `/opt/phoenix/renew-cert.sh`:
1. Stops `phoenix-oci-web` (releases port 80 for ~30 seconds)
2. Runs `certbot/certbot renew --standalone` in Docker
3. Restarts `phoenix-oci-web` — the container entrypoint re-renders the nginx config from
   `/run/secrets/admin_api_key` automatically

Logs: `/opt/phoenix/logs/cert-renewal.log`

### Manual renewal

```bash
docker stop phoenix-oci-web

docker run --rm \
  -p 80:80 \
  -v /opt/phoenix/certs:/etc/letsencrypt \
  certbot/certbot renew --standalone --non-interactive --agree-tos

# Restart nginx — internal envsubst handles ADMIN_API_KEY from /run/secrets/
cd /opt/phoenix/app
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-deps --force-recreate nginx
```

> **After ADMIN_API_KEY rotation:** Simply force-recreate the nginx container (command above).
> The container reads the updated secret from `/run/secrets/admin_api_key` at startup.
> No host-side config file needs to be updated.

---

## Patches applied to source tree

All patches are tracked in git and baked into the OCIR image at build time.
**No bind mounts of individual source files are required or permitted.**

| File | Fix |
|---|---|
| `app/core/angel_login.py` | `ANGEL_HTTPS_PROXY` / `HTTPS_PROXY` env support in `_make_angel_connection()` |
| `app/core/order_client.py` | Same proxy support for all order API calls |
| `app/core/universe_builder.py` | Same proxy support for universe quote calls |
| `app/core/ws_runner.py` | WebSocket proxy via `http_proxy_host/port` + `proxy_type="http"` |
| `app/runtime/app_runtime.py` | Watchdog backoff: `2.0 **` to prevent `OverflowError` at high restart counts |
| `migrations/014_broker_credentials_state.sql` | `ALTER TABLE broker_credentials ADD COLUMN state JSONB` |
| `nginx/nginx-ssl.conf.template` | Dual-server config: port 80 redirect + port 8443 TLS with security headers |

---

## Common failures

### `/readyz` returns 503 `no_runners_registered` during market hours

1. Check `stream_worker_running` in `/health` — if `false`, stream worker crashed
2. Check logs for `Stream worker crashed` — likely Angel One login or WebSocket timeout
3. Check proxy: `systemctl is-active tinyproxy` on the proxy VM
4. Check Angel One portal — confirm `<NAT_GATEWAY_IP>` is still in the IP whitelist

### WebSocket disconnects every 2 minutes

Cause: Angel One's firewall blocking direct WebSocket from OCI IP — proxy not being used.

```bash
docker exec phoenix-oci-backend env | grep ANGEL_HTTPS_PROXY
# Must show http://<PROXY_IP>:8888
```

If missing, `ANGEL_HTTPS_PROXY` is not in the override — add it to `phoenix-override.yml`.

### `Failed to load broker credentials from Postgres for broker_account_id=...`

Migration 014 not applied:
```bash
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix \
  -c "ALTER TABLE broker_credentials ADD COLUMN IF NOT EXISTS state JSONB NOT NULL DEFAULT '{}';"
docker restart phoenix-oci-backend
```

### Container startup fails with `Startup runtime setting validation failed`

Check the full error. Common causes:
- `CAPITAL_LIMITS_JSON={}` — set real per-account capital limits, remove `ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY`
- `CONTROL_PLANE_DB_DSN` SSL mismatch — override must match actual Postgres SSL config

### HTTPS returns SSL handshake error or connection reset

The nginx entrypoint may have overwritten the SSL config with the HTTP-only template.

```bash
docker exec phoenix-oci-web grep 'listen 8443' /etc/nginx/conf.d/default.conf
# If empty — the SSL block is missing; force-recreate:
cd /opt/phoenix/app
docker compose -f docker-compose.oci-live.yml -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env up -d --no-deps --force-recreate nginx
```

### TLS cert expired or near expiry

```bash
echo | openssl s_client -connect <PHOENIX_DOMAIN>:443 2>/dev/null | openssl x509 -noout -dates
# If expired, run /opt/phoenix/renew-cert.sh manually
```

### Bastion session expired

Create a new session with the PowerShell command above. Sessions expire after 3 hours.

---

## Stopping and restarting

```bash
# Restart backend only
docker restart phoenix-oci-backend

# Full redeploy from compose
cd /opt/phoenix/app
docker compose -f docker-compose.oci-live.yml -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env up -d --no-deps backend

# Stop everything
docker compose -f docker-compose.oci-live.yml down --remove-orphans
```

---

## Updating broker credentials

Use the [update_broker_credentials.md](update_broker_credentials.md) runbook.
After updating `broker_credentials` in Postgres, restart the backend:

```bash
docker restart phoenix-oci-backend
```
