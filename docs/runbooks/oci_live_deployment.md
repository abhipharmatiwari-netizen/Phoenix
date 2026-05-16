# Phoenix OCI LIVE Deployment

> **Status:** OCI Compose operator runbook for the repo-tracked `docker-compose.oci-live.yml` path.
>
> **Approval state:** Not approved for unattended LIVE by documentation alone. Operators must capture release evidence from the running OCI backend and confirm the production contract in `ARCHITECTURE.md`.

## Purpose

Deploy Phoenix on an OCI VM using OCIR images, OCI Vault/file-backed Docker secrets, nginx, and a Postgres control-plane endpoint. This runbook describes only the deployment model represented by files in this repo.

## Scope

This runbook covers:

- `docker-compose.oci-live.yml`
- `phoenix-override.yml.example`
- `docs/runbooks/oci-live.env.example`
- `scripts/fetch-secrets.sh`
- `scripts/start-phoenix.sh` and `scripts/stop-phoenix.sh`

It does not approve Cloud Run, Firestore authority, repo-stored secrets, local env-file secrets, or ad-hoc VM state that is not represented by the repo-tracked manifest and release evidence.

## Preconditions

- OCI VM can pull from OCIR.
- `IMAGE_TAG` is a specific git SHA, not `latest`.
- The VM has both backend and nginx images for `IMAGE_TAG` locally before any scheduled start. `scripts/start-phoenix.sh` refuses to run `docker compose up` if either image is missing.
- Postgres endpoint is reachable from the backend and migrator containers.
- `CONTROL_PLANE_PG_SSLMODE=require` for remote Postgres. Do not set `LIVE_PG_SSL_SKIP_CHECK=true` on remote/cloud Postgres.
- `/run/secrets/` contains non-empty `admin_api_key`, `auth_token_secret`, `control_plane_pg_password`, and `angel_postback_token`.
- Control-plane rows exist for tenant, broker account, subscription, strategy configs, and `broker_credentials`.
- `CAPITAL_LIMITS_JSON` contains funded-account-specific limits.
- `CLIENT_LOCAL_IP`, `CLIENT_PUBLIC_IP`, and `MAC_ADDRESS` match the broker-approved network identity.
- `PHOENIX_DOMAIN` is set when nginx TLS config is used.

## Required Environment Variables

Use `docs/runbooks/oci-live.env.example` as the non-secret template for `/opt/phoenix/phoenix-deploy.env`.

Required values:

- `OCIR_NAMESPACE`
- `OCIR_REGION`
- `IMAGE_TAG`
- `CONTROL_PLANE_PG_HOST`
- `CONTROL_PLANE_PG_DB`
- `CONTROL_PLANE_PG_USER`
- `HUB_DEFAULT_TENANT_ID` — default tenant used by hub routing, strategy_bridge, and trade_records when no tenant is provided by the caller. Must match the production tenant UUID. Required in LIVE (startup validation rejects empty value). The bundled compose example defaults to `tenant-1`; set it explicitly in your `phoenix-deploy.env`.
- `HUB_DEFAULT_BROKER_ACCOUNT_ID`
- `CAPITAL_LIMITS_JSON`
- `RISK_MAX_DAILY_LOSS` — one-sided daily realised+unrealised loss cap in INR (trips the kill switch only when the day's running P&L falls **below** `-abs(value)`; profitable days are unaffected). **Must be sized per capital tier** — see [Sizing the daily-loss limit by capital tier](#sizing-the-daily-loss-limit-by-capital-tier) below. LIVE startup fails closed when this is below `RISK_MAX_DAILY_LOSS_LIVE_FLOOR` (default ₹5,000) — set explicitly in `/opt/phoenix/phoenix-deploy.env`. The committed `.env.example`/`cloudrun.env` placeholders are `CHANGE_ME_DAILY_LOSS_INR` and will not start LIVE.
- `PROFIT_DAILY_TARGET`
- `CLIENT_LOCAL_IP`
- `CLIENT_PUBLIC_IP`
- `MAC_ADDRESS`
- `PHOENIX_DOMAIN`
- `PHOENIX_LOG_HOST_PATH`
- `PHOENIX_STATE_HOST_PATH`
- `PHOENIX_CERTS_HOST_PATH`

Secrets are files under `/run/secrets/`, not values committed to env templates.

## Building and Pushing a New Image

Two scripts are available. Use only one per deployment.

### Token-based (operator auth token from OCI Console)

```bash
OCIR_AUTH_TOKEN=<token> OCIR_USERNAME=<ns>/<domain>/<user> \
  sh /opt/phoenix/app/scripts/ops/build_and_push_image.sh
```

### Instance-principal (VM IAM policy, token from OCI Vault)

```bash
OCIR_NAMESPACE=<ns> OCIR_USERNAME=<ns>/<domain>/<user> \
  VAULT_SECRET_OCID=<ocid-of-vault-secret-for-ocir-token> \
  sh /opt/phoenix/app/scripts/ops/build_push_ip.sh
```

Both scripts:
- Verify pre-conditions before building
- Build the backend and nginx `linux/amd64` images tagged with the same current git SHA
- Push both images to OCIR and update `IMAGE_TAG` in `phoenix-deploy.env`
- Print the SHA on completion; they do **not** restart containers

Do not update `IMAGE_TAG` after building only one service image. The backend and nginx images are a release pair; splitting the build can leave the next scheduled start unable to recreate nginx.

After push, **separately** run:

```bash
sh /opt/phoenix/app/scripts/ops/redeploy_backend.sh
```

`redeploy_backend.sh` prompts for confirmation, pulls the backend and nginx images pinned in `phoenix-deploy.env`, restarts the backend, polls `/readyz` with a 120-second timeout, then recreates nginx on the same `IMAGE_TAG`.

---

## Secret Refresh

Run on the OCI VM as the operator user with instance-principal OCI access:

```bash
OCI_CLI_BIN=/home/opc/bin/oci \
OCI_VAULT_ID=<VAULT_OCID> \
  sh /opt/phoenix/app/scripts/fetch-secrets.sh
```

Validation:

```bash
sudo test -s /run/secrets/admin_api_key
sudo test -s /run/secrets/auth_token_secret
sudo test -s /run/secrets/control_plane_pg_password
sudo test -s /run/secrets/angel_postback_token
```

Expected success evidence: every file exists and is non-empty. If any secret fetch fails, `fetch-secrets.sh` leaves the previous file unchanged and returns an error.

## Migration and Preflight

From `/opt/phoenix/app`:

```bash
CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  run --rm migrator
```

```bash
CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  run --rm db-preflight
```

Do not start the backend if either command fails.

## Deploy

```bash
cd /opt/phoenix/app

CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-build --force-recreate backend nginx
```

For scheduled starts and stops, install the repo-tracked scripts as VM cron jobs only after validating they point at the same compose and env files:

```bash
sudo /opt/phoenix/start-phoenix.sh
sudo /opt/phoenix/stop-phoenix.sh
```

Scheduled starts fail fast before any compose start/recreate action if the pinned backend or nginx image is not already present locally. For OCIR deployments, `redeploy_backend.sh` performs the pull. For local-only recovery tags such as `local-<sha>`, build both `phoenix-local-backend:<tag>` and `phoenix-local-nginx:<tag>` before the next cron start.

The `backend-watchdog` service is observe-only. It logs backend health transitions but does not start or stop nginx; backend-down traffic draining is handled by nginx `/readyz` and the OCI load balancer health check.

## Validation

Container state:

```bash
docker compose \
  -f /opt/phoenix/app/docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  ps
```

Health:

```bash
curl -fsS https://<PHOENIX_DOMAIN>/health
curl -fsS https://<PHOENIX_DOMAIN>/health/summary
curl -fsS https://<PHOENIX_DOMAIN>/readyz
```

Effective backend environment:

```bash
docker exec phoenix-oci-backend sh -lc \
  "env | egrep '^(TRADE_MODE|REQUIRE_LIVE_TRADE_MODE|ENABLE_MULTI_HUB|USE_HUB_ROUTER|DISABLE_STREAM_WORKER|BROKER_SECRET_BACKEND|CONTROL_PLANE_BACKEND|SWEEP_STATE_BACKEND|APP_RUNTIME_STARTUP_VALIDATE|SCHEMA_CHECK_MODE|BROKER_SCHEMA_CHECK_MODE|DASHBOARD_AUTH_DISABLED|DISABLE_CONTROL_TOWER_ROUTES|ORDER_ROUTER_ENFORCE_IDEMPOTENCY|POSITION_OWNERSHIP_ENABLED|ENABLE_EOD_EXIT|RISK_STATE_PATH|CONTROL_PLANE_PG_SSLMODE|LIVE_PG_SSL_SKIP_CHECK)='"
```

Expected success evidence:

- `TRADE_MODE=LIVE`
- `REQUIRE_LIVE_TRADE_MODE=true`
- `ENABLE_MULTI_HUB=true`
- `USE_HUB_ROUTER=true`
- `DISABLE_STREAM_WORKER=false`
- `CONTROL_PLANE_BACKEND=postgres`
- `SWEEP_STATE_BACKEND=postgres`
- `BROKER_SECRET_BACKEND=postgres` or another architecture-approved secret backend
- `/readyz` returns 200
- `running_runner_count >= 1`
- `stream_worker_running=true`
- `balance_sync_ready=true`
- `kill_switch_active_count=0`

Release evidence:

```bash
ADMIN_KEY="$(sudo cat /run/secrets/admin_api_key)"
curl -fsS -H "X-Admin-Key: ${ADMIN_KEY}" \
  https://<PHOENIX_DOMAIN>/admin/release-evidence
```

Attach the JSON to the deployment record and review it against `docs/runbooks/release_evidence.md`.

## Expected Warnings

The following can be expected only when explicitly justified in the deployment record:

- `startup.ssl_warning` is acceptable only for local loopback Postgres. It is not acceptable for remote/cloud Postgres.
- `startup.angel_postback_token_missing` means broker postbacks return 401 and lifecycle convergence relies on polling. This is not acceptable for a normal OCI LIVE deployment because the compose manifest mounts `angel_postback_token`.

The following are blockers:

- `strategy.unroutable`
- `BROKER_SCHEMA_VIOLATION`
- `leader_lease_not_owned` on the intended writer
- `balance_sync_not_ready` after the broker maintenance window
- `stream_worker_not_running`
- `startup_ownership_gap_detected`

## Failure Handling

- Migrator/preflight failure: stop; fix schema, connectivity, or control-plane rows; rerun preflight.
- Backend startup validation failure: do not bypass; fix the env or durable backend.
- `/readyz` 503: inspect the `reason` field and backend logs before allowing entries.
- Broker connectivity failure: verify broker portal whitelist, proxy configuration, and SmartAPI status.
- Missing secrets: rerun `scripts/fetch-secrets.sh`; do not paste secrets into repo files.

## Rollback / Recovery

If the new backend is unhealthy:

```bash
cd /opt/phoenix/app
IMAGE_TAG=<previous_git_sha>  # edit /opt/phoenix/phoenix-deploy.env

CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-build --force-recreate backend nginx
```

Then repeat the validation and release-evidence steps. Do not resume automated LIVE until `/readyz` and release evidence pass.

## Ownership

The operator on duty owns:

- choosing the exact `IMAGE_TAG`
- maintaining `/opt/phoenix/phoenix-deploy.env`
- refreshing `/run/secrets/`
- proving Postgres migrations and control-plane rows
- capturing release evidence
- holding or rolling back on any readiness or reconciliation blocker

---

## Sizing the daily-loss limit by capital tier

`RISK_MAX_DAILY_LOSS` is the **absolute daily realised + unrealised loss limit in INR** — the one-sided cap on how far the day's running P&L is allowed to fall below zero. The risk engine compares the running P&L against `-abs(RISK_MAX_DAILY_LOSS)` (see [`app/risk/risk_engine.py`](../../app/risk/risk_engine.py) and [`app/core/risk_manager.py`](../../app/core/risk_manager.py)); the kill switch trips only when the running P&L crosses below that negative threshold. A profitable day giving back gains, or a day finishing positive, does **not** trip this gate. Once the gate trips it blocks new entries (SOFT) or all orders including exits (HARD), and the exit engine subsequently squares off open positions per its policy.

**Why this matters (issue #221, 2026-05-08 incident).** The historical committed default of ₹2,000 tripped the kill switch on a ~1.6-point adverse mark-to-market move on a single NG_FUT lot. A limit that fires on routine intraday volatility is operationally meaningless — operators learn to re-arm it, distorting backtests-vs-live and leaving real risk un-bounded.

**Hard gate.** Phoenix LIVE startup now fails closed when this value is below `RISK_MAX_DAILY_LOSS_LIVE_FLOOR` (default ₹5,000). To deliberately accept a sub-floor value (e.g. for a very small paper-funded account), set `RISK_MAX_DAILY_LOSS_LIVE_FLOOR` explicitly to a lower value or to `0` (disables the gate).

**Sizing guidance.** A daily-loss limit should be:

- **Large enough** to absorb normal intraday volatility plus 1–2 typical losing-trade exits without tripping prematurely.
- **Small enough** that hitting it represents a clearly anomalous day worth stopping.

A pragmatic target is **3–5% of account capital**, capped so that no single overnight gap can produce a worse outcome than the limit. Use the table below as a starting point and adjust per realised volatility of the instruments traded.

| Account capital tier | Suggested `RISK_MAX_DAILY_LOSS` | Rationale |
|---|---|---|
| ₹1L – ₹2L (small / paper-funded) | **₹10,000** | ~5–10% of capital; absorbs an ~8-point NG_FUT adverse move on 1 lot before tripping. |
| ₹5L | ₹15,000 – ₹20,000 | ~3–4% of capital; comfortably absorbs a 2-leg credit-spread reversal. |
| ₹10L – ₹25L | ₹30,000 – ₹75,000 | ~3% of capital; tolerates multi-strategy concurrent drawdowns. |
| ₹25L+ | ₹1,00,000+ | Operator decides per capital tier and risk policy. |

**Operator workflow:**

1. Decide the production value for the deploying VM (typically picked once per account at provisioning).
2. Set it explicitly in `/opt/phoenix/phoenix-deploy.env` — there is no fallback in `docker-compose.live.single.yml` or `docker-compose.oci-live.yml` (both use `?Set RISK_MAX_DAILY_LOSS`). Startup fails fast if the value is missing or below the floor.
3. Capture the chosen value in deploy notes alongside `PROFIT_DAILY_TARGET` so the limit ratio is auditable.
4. Re-evaluate after the first 2 weeks of trading using realised P&L distribution — if the limit fires on > 10% of trading days the threshold is likely too tight.

**Verification:** After deploy, confirm the running value:

```bash
docker inspect phoenix-oci-backend \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep RISK_MAX_DAILY_LOSS
```

---

## Override File Migration (one-time, from pre-2026-05-07 deployments)

If the VM is running a `phoenix-override.yml` created before 2026-05-07, apply these
changes before the next deploy. Each change is independent — do not skip any.

| Old value | New value | Why |
|---|---|---|
| `LIVE_PG_SSL_SKIP_CHECK: "true"` | Remove the line | Remote Postgres must use SSL |
| `CONTROL_PLANE_PG_SSLMODE: "prefer"` | `CONTROL_PLANE_PG_SSLMODE: "require"` | Enforce SSL |
| `REMOTE_DEPLOYMENT: "false"` | `REMOTE_DEPLOYMENT: "true"` | Correct runtime classification |
| `CONTROL_PLANE_DB_DSN: "postgresql://phoenix_app@phoenix-oci-postgres:5432/phoenix"` | Remove this line (use env-file PG vars instead) | Container-local Postgres is no longer used |
| `ANGEL_HTTPS_PROXY` / `HTTPS_PROXY` hardcoded to proxy IP | Comment out if Angel One accepts the OCI IP directly; otherwise update to current proxy IP | Config drift |
| 8 source-code bind mounts under `volumes:` | Remove all of them | Deploy pinned OCIR image; no code overlays |
| nginx `volumes: /opt/phoenix/nginx-ssl-prerendered.conf.template:/tmp/nginx.conf.template:ro` | Change to `/opt/phoenix/app/nginx/nginx-ssl.conf.template:/tmp/nginx.conf.template:ro` | Use repo-tracked template directly |

After updating the override file, validate that compose config resolves without errors:

```bash
CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
docker compose \
  -f /opt/phoenix/app/docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  config --quiet
```

Then remove `nginx-ssl-prerendered.conf.template` from the VM if it is no longer referenced:

```bash
ls -la /opt/phoenix/nginx-ssl-prerendered.conf.template
# Remove only after verifying nginx container uses the repo template
# sudo rm /opt/phoenix/nginx-ssl-prerendered.conf.template
```

Test Postgres SSL connectivity before cutting over:

```bash
PGPASSWORD=$(sudo cat /run/secrets/control_plane_pg_password) \
  psql "postgresql://phoenix_app@<CONTROL_PLANE_PG_HOST>:5432/phoenix?sslmode=require" \
  -c "SELECT 1;"
```
