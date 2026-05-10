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
- `RISK_MAX_DAILY_LOSS`
- `PROFIT_DAILY_TARGET`
- `CLIENT_LOCAL_IP`
- `CLIENT_PUBLIC_IP`
- `MAC_ADDRESS`
- `PHOENIX_DOMAIN`
- `PHOENIX_LOG_HOST_PATH`
- `PHOENIX_STATE_HOST_PATH`
- `PHOENIX_CERTS_HOST_PATH`

Secrets are files under `/run/secrets/`, not values committed to env templates.

## Sizing daily-loss limit by capital tier

`RISK_MAX_DAILY_LOSS` is an operator-owned LIVE value and must not be inherited
from repo defaults. The current production sizing policy is:

```text
RISK_MAX_DAILY_LOSS = 1% of account capital
```

Use the funded account capital for the tenant/broker account that will carry the
positions, then round deliberately to an auditable INR amount. Examples:

| Account capital | 1% daily-loss limit | Example setting |
| ---: | ---: | --- |
| ₹5,00,000 | ₹5,000 | `RISK_MAX_DAILY_LOSS=5000` |
| ₹20,00,000 | ₹20,000 | `RISK_MAX_DAILY_LOSS=20000` |
| ₹50,00,000 | ₹50,000 | `RISK_MAX_DAILY_LOSS=50000` |
| ₹2,00,00,000 | ₹2,00,000 | `RISK_MAX_DAILY_LOSS=200000` |

Operational notes:

- Record the capital tier and selected `RISK_MAX_DAILY_LOSS` in the deploy notes
  before restarting the LIVE backend.
- Keep `RISK_LIVE_MIN_DAILY_LOSS_INR` at or above the smallest approved capital
  tier's 1% limit; the default floor is ₹5,000.
- Leave `RISK_LIVE_LOW_DAILY_LOSS_ACTION=warn` only during rollout. Set it to
  `error` once all LIVE profiles have an approved capital-tier value so startup
  fails closed on suspiciously low daily-loss limits.

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
- Build a `linux/amd64` image tagged with the current git SHA
- Push to OCIR and update `IMAGE_TAG` in `phoenix-deploy.env`
- Print the SHA on completion — they do **not** restart the container

After push, **separately** run:

```bash
sh /opt/phoenix/app/scripts/ops/redeploy_backend.sh
```

`redeploy_backend.sh` prompts for confirmation, pulls the image pinned in `phoenix-deploy.env`,
restarts the backend, and polls `/readyz` with a 120-second timeout before returning.

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
