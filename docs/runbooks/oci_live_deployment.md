# Phoenix OCI Live Deployment

> **Status:** Active production deployment runbook for the Oracle Cloud Infrastructure (OCI) path.
> Supersedes any ad-hoc cloud notes. Covers first-time setup, normal redeploy, and troubleshooting.

---

## Architecture overview

```
Internet (Angel One SmartAPI / WebSocket)
        │
        ▼
Vultr Mumbai VPS — phoenix-proxy (65.20.69.50)
  tinyproxy :8888  ◄── only accepts from OCI NAT IP 141.148.216.169
        │
        ▼ (HTTPS CONNECT tunnel)
OCI Mumbai — phoenix-vm (10.0.2.83, private subnet)
  NAT Gateway outbound IP: 141.148.216.169
        │
  docker phoenix-oci-backend  (image from OCIR)
  docker phoenix-oci-web      (nginx, port 80)
  docker phoenix-oci-postgres (Postgres)
        │
        ▼ (port 80 inbound)
OCI Load Balancer — phoenix-lb (161.118.189.93, public subnet)
        │
        ▼
Internet users / Angel One postback
```

**Why the proxy?** Angel One's SmartAPI firewall blocks OCI's cloud IP ranges at the TCP level.
All Angel One HTTP (REST) and WebSocket traffic is tunnelled through the Vultr proxy via HTTP CONNECT.

---

## Key infrastructure IDs

| Resource | Value |
|---|---|
| OCI VM private IP | `10.0.2.83` |
| OCI NAT gateway outbound IP | `141.148.216.169` |
| OCI Load Balancer public IP | `161.118.189.93` |
| Vultr proxy IP | `65.20.69.50` |
| Proxy port | `8888` |
| OCI compartment OCID | `ocid1.compartment.oc1..aaaaaaaajupjauxguwhkb7j75nbqv5qbkuid7tyvezsgev2u3v4e6uh3cyfq` |
| OCI instance OCID | `ocid1.instance.oc1.ap-mumbai-1.anrg6ljrn3xpydyctryr2pawrawrp4kruunixipvhkyh3ccihceazyavgbsa` |
| OCI bastion OCID | `ocid1.bastion.oc1.ap-mumbai-1.amaaaaaan3xpydyaahnxwoa5ro4vtd5wr6pv7cno46jr6lq52iud6ri4bqia` |
| OCI LB OCID | `ocid1.loadbalancer.oc1.ap-mumbai-1.aaaaaaaahowdi4iynfvkgbvmxes2bmzyv4avwtwbanepijep3blw33gp2gnq` |
| OCIR namespace | `bmfve1wf5neh` |
| OCIR region | `ap-mumbai-1` |

---

## Angel One developer portal settings

Log into [smartapi.angelbroking.com](https://smartapi.angelbroking.com) and configure the API app:

| Field | Value |
|---|---|
| **IP Whitelist** | `141.148.216.169` (OCI NAT gateway) |
| **Redirect URL** | `http://161.118.189.93/` |
| **Postback URL** | `http://161.118.189.93/webhook/angel/postback` |

The postback URL receives Angel One order fill events. Without it, the lifecycle service
never receives fills and positions remain unreconciled.

---

## SSH access via OCI Bastion

Bastion sessions expire. Create a new one whenever SSH access is needed:

```bash
# From local Windows machine (PowerShell)
$pubKey = Get-Content "D:\0 AMP\Phoenix_Secrets\ssh-key-2026-04-27.key.pub" -Raw
$pubKey.Trim() | Out-File "$env:TEMP\pub.txt" -NoNewline -Encoding ascii

$session = oci bastion session create-managed-ssh `
  --bastion-id ocid1.bastion.oc1.ap-mumbai-1.amaaaaaan3xpydyaahnxwoa5ro4vtd5wr6pv7cno46jr6lq52iud6ri4bqia `
  --target-resource-id ocid1.instance.oc1.ap-mumbai-1.anrg6ljrn3xpydyctryr2pawrawrp4kruunixipvhkyh3ccihceazyavgbsa `
  --target-os-username opc `
  --ssh-public-key-file "$env:TEMP\pub.txt" `
  --session-ttl 10800 `
  --display-name phoenix-session `
  --region ap-mumbai-1 2>&1 | ConvertFrom-Json
$SESSION_ID = $session.data.id

# Wait for ACTIVE
until (oci bastion session get --session-id $SESSION_ID --region ap-mumbai-1 2>/dev/null | grep -q '"ACTIVE"'); do sleep 5; done
```

Then SSH:

```bash
KEY="D:/0 AMP/Phoenix_Secrets/ssh-key-2026-04-27.key"
ssh -i "$KEY" \
  -o "ProxyCommand=ssh -i \"$KEY\" -W %h:%p -p 22 ${SESSION_ID}@host.bastion.ap-mumbai-1.oci.oraclecloud.com" \
  -p 22 -o StrictHostKeyChecking=no opc@10.0.2.83
```

---

## Files on the OCI VM

All persistent deployment state lives under `/opt/phoenix/`:

```
/opt/phoenix/
├── app/                          # Git checkout of Phoenix repo (source of truth for patched files)
│   ├── app/core/angel_login.py   # Patched: ANGEL_HTTPS_PROXY support
│   ├── app/core/order_client.py  # Patched: ANGEL_HTTPS_PROXY support
│   ├── app/core/universe_builder.py  # Patched: ANGEL_HTTPS_PROXY support
│   ├── app/core/ws_runner.py     # Patched: WebSocket proxy_type support
│   ├── app/runtime/app_runtime.py    # Patched: watchdog backoff overflow fix
│   └── docker-compose.oci-live.yml   # Base compose manifest (OCIR images)
├── phoenix-override.yml          # Compose override: bind mounts + env overrides
├── phoenix-deploy.env            # Required env vars for docker compose
├── state/                        # Persistent runtime state (risk, positions)
├── certs/                        # TLS certificates
└── logs/                         # Application logs
```

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
- `ANGEL_HTTPS_PROXY=http://65.20.69.50:8888` as a real container env var
- Bind mounts for all 5 patched source files from the host source tree
- `CONTROL_PLANE_DB_DSN` without SSL (local Postgres container)
- `ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY=true`

### After code changes

1. SCP the changed file(s) to `/opt/phoenix/app/app/...` on the VM
2. Run the deploy command above — the bind mount picks up the new file automatically

---

## Proxy server (Vultr)

The proxy is a Vultr Mumbai VPS running tinyproxy.

### Check status

```bash
python3 << 'EOF'
import paramiko
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("65.20.69.50", username="root", password="<password>", timeout=30)
stdin, stdout, stderr = client.exec_command("systemctl is-active tinyproxy && ss -tlnp | grep 8888")
print(stdout.read().decode())
client.close()
EOF
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
Allow 141.148.216.169
```

`Restart=always` is set via `/etc/systemd/system/tinyproxy.service.d/restart.conf` —
tinyproxy auto-restarts on crash and on VM reboot.

### UFW rules

Port 8888 only accepts connections from `141.148.216.169` (OCI NAT IP):
```bash
ufw status
# 8888    ALLOW   141.148.216.169
# 22/tcp  ALLOW   Anywhere
```

---

## Database initial seed (first-time only)

If the control-plane DB is empty (fresh deployment), seed the required rows:

```bash
# On the OCI VM
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix -c "
INSERT INTO tenants (tenant_id, name, email, phone, status, notes)
VALUES ('tenant-1', 'Phoenix Live', 'abhipharma.tiwari@gmail.com', '', 'active', 'Primary live trading tenant')
ON CONFLICT (tenant_id) DO NOTHING;

INSERT INTO broker_accounts (broker_account_id, tenant_id, broker_type, display_name, client_code, secret_ref, trading_mode, enabled)
VALUES ('A1', 'tenant-1', 'angel', 'Angel One A1', 'A92268', 'A1', 'LIVE', true)
ON CONFLICT (broker_account_id) DO NOTHING;

INSERT INTO subscriptions (subscription_id, tenant_id, broker_account_id, mode, start_at, end_at)
VALUES ('sub-A1-live', 'tenant-1', 'A1', 'LIVE', now(), '2099-12-31 23:59:59+00')
ON CONFLICT (subscription_id) DO NOTHING;

INSERT INTO strategy_configs (strategy_config_id, tenant_id, broker_account_id, strategy_id, enabled, params)
VALUES
  ('sc-ema20',       'tenant-1', 'A1', 'ema20_strategy',        true, '{}'),
  ('sc-ce-buy',      'tenant-1', 'A1', 'exclusive_nifty_ce_buy',true, '{}'),
  ('sc-put-scalper', 'tenant-1', 'A1', 'put_momentum_scalper',  true, '{}')
ON CONFLICT (strategy_config_id) DO NOTHING;
"
```

Broker credentials (`broker_credentials` table) must already be populated.
The `state` column (migration `014`) must be present — run pending migrations first.

---

## Verification after deploy

### 1. Health and readiness

```bash
# From local machine
curl http://161.118.189.93/health | python3 -m json.tool
curl http://161.118.189.93/readyz | python3 -m json.tool
```

Expected for a fully healthy system:
- `/health`: `"stream_worker_running": true, "watchdog_running": true`
- `/readyz`: `"ready": true, "runner_count": 1, "running_runner_count": 1`

### 2. Angel One connectivity

```bash
# From OCI VM
timeout 6 bash -c 'cat < /dev/null > /dev/tcp/apiconnect.angelone.in/443' \
  && echo 'DIRECT: OPEN' || echo 'DIRECT: BLOCKED (expected — routed via proxy)'

# Confirm proxy is forwarding
docker exec phoenix-oci-backend curl -s --max-time 8 \
  -x http://65.20.69.50:8888 \
  https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword \
  -o /dev/null -w "%{http_code}"
# Expected: 200 or 400 (not a timeout)
```

### 3. Live tick stream

```bash
docker logs phoenix-oci-backend --tail 20 2>&1 | grep "BAR CLOSED"
# Should show bars closing with RSI/MACD/ATR values during market hours (09:15–15:30 IST)
```

### 4. Readyz outside market hours

Outside market hours, `/readyz` returns `503 no_runners_registered`. This is expected —
runners only register when the stream worker is active and has a healthy WebSocket session.
During market hours `runner_count` should be ≥ 1.

---

## Known permanent fixes applied to source tree

These patches are bind-mounted from `/opt/phoenix/app/app/` into the container.
They must be kept in the VM source tree and are tracked in the Git repo.

| File | Fix |
|---|---|
| `app/core/angel_login.py` | `ANGEL_HTTPS_PROXY` / `HTTPS_PROXY` support in `_make_angel_connection()` |
| `app/core/order_client.py` | Same proxy support for all order API calls |
| `app/core/universe_builder.py` | Same proxy support for universe quote calls |
| `app/core/ws_runner.py` | WebSocket proxy via `http_proxy_host/port` + `proxy_type="http"` in `run_forever()` |
| `app/runtime/app_runtime.py` | Watchdog backoff: `2 **` → `2.0 **` to prevent `OverflowError` at ~1038 restart attempts |
| `migrations/014_broker_credentials_state.sql` | `ALTER TABLE broker_credentials ADD COLUMN state JSONB NOT NULL DEFAULT '{}'` |

---

## Common failures

### `/readyz` returns 503 `no_runners_registered` during market hours

1. Check `stream_worker_running` in `/health` — if `false`, stream worker crashed
2. Check logs for `Stream worker crashed` — likely Angel One login or WebSocket timeout
3. Check proxy: `systemctl is-active tinyproxy` on `65.20.69.50`
4. Check Angel One portal — confirm `141.148.216.169` is still in the IP whitelist

### WebSocket disconnects every 2 minutes

Symptom: `Connection closed due to max retry attempts reached` in logs every ~120 seconds.
Cause: Angel One's firewall blocking direct WebSocket from OCI IP — proxy not being used.
Fix: Ensure `ws_runner.py` bind mount is active and `ANGEL_HTTPS_PROXY` is set.

```bash
docker exec phoenix-oci-backend grep -c 'proxy_type' /app/app/core/ws_runner.py
# Must be > 0
docker exec phoenix-oci-backend env | grep ANGEL_HTTPS_PROXY
# Must show http://65.20.69.50:8888
```

### `Hub route validation failed; missing routes for: ema20_strategy@...`

Cause: `strategy_configs` table empty or missing the required strategy IDs.
Fix: Run the database seed SQL above.

### `Failed to load broker credentials from Postgres for broker_account_id=A1`

Cause: `broker_credentials.state` column missing — migration 014 not applied.
Fix:
```bash
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix \
  -c "ALTER TABLE broker_credentials ADD COLUMN IF NOT EXISTS state JSONB NOT NULL DEFAULT '{}';"
docker restart phoenix-oci-backend
```

### `Stream watchdog failed to restart worker: int too large to convert to float`

Cause: Old image without the `2.0 **` fix, or `app_runtime.py` bind mount not active.
Fix: Ensure `/opt/phoenix/app/app/runtime/app_runtime.py` is the patched version and
the bind mount is in `phoenix-override.yml`.

### Container startup fails with `Startup runtime setting validation failed`

Check the full error message. Common causes:
- `ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY=true` not in override → add to `phoenix-override.yml`
- `CONTROL_PLANE_DB_DSN` using `sslmode=require` but local Postgres has no SSL → override in `phoenix-override.yml`

### Bastion session expired

Create a new session with the command above. Sessions expire after 3 hours (`--session-ttl 10800`).

---

## Stopping and restarting

```bash
# Restart backend only (picks up bind-mounted file changes)
docker restart phoenix-oci-backend

# Full redeploy from compose (rebuilds container config)
cd /opt/phoenix/app
docker compose -f docker-compose.oci-live.yml -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env up -d --no-deps backend

# Stop everything
docker compose -f docker-compose.oci-live.yml down --remove-orphans
```

---

## Updating broker credentials

Use the existing [update_broker_credentials.md](update_broker_credentials.md) runbook.
After updating `broker_credentials` in Postgres, restart the backend:

```bash
docker restart phoenix-oci-backend
```

Also update `CLIENT_PUBLIC_IP=141.148.216.169` and `MAC_ADDRESS` in the `broker_credentials`
row if the VM or network changes.
