# Phoenix OCI LIVE Deployment Runbook

Status: current operator runbook for the OCI VM verified on 2026-05-17.

This runbook describes what is actually running on the OCI VM. It does not
describe the older intended OCIR/external-Postgres deployment as current state.

## Purpose

Operate Phoenix on the OCI VM without exposing secrets or changing live trading
state accidentally.

## Scope

Current VM paths and containers:

- repo checkout: `/opt/phoenix/app`
- Compose files: `/opt/phoenix/app/docker-compose.oci-live.yml` and `/opt/phoenix/phoenix-override.yml`
- env file: `/opt/phoenix/phoenix-deploy.env`
- backend: `phoenix-oci-backend`
- web/nginx: `phoenix-oci-web`
- database: `phoenix-oci-postgres`
- watchdog: `phoenix-oci-watchdog`
- logs: `/opt/phoenix/logs`
- state helpers: `/opt/phoenix/state`
- certs: `/opt/phoenix/certs`
- OI/ML shadow checkout: `/opt/phoenix/oi-ml-shadow-src`
- OI/ML shadow compose: `/opt/phoenix/oi-ml-shadow.yml`
- OI/ML shadow container: `phoenix-oi-ml-shadow`

Non-current for this VM unless a later evidence capture proves otherwise:

- OCIR images
- external OCI Database for PostgreSQL
- Docker Desktop deployment
- Cloud Run/GCP deployment
- Firestore or BigQuery as operational authority
- optimizer and backend-reload systemd timers

## OI/ML Shadow Sidecar

The OI/ML CE seller sidecar is separate from the live backend/nginx stack. It is
dry-run only, publishes no host ports, and records option-chain snapshots plus
shadow order intents in Postgres. It must not be used as evidence that live order
routing is enabled.

Current sidecar evidence as of 2026-05-18:

- image: `phoenix-oi-ml-shadow:oi-ml-shadow-9e91b77`
- checkout: `/opt/phoenix/oi-ml-shadow-src`
- compose: `/opt/phoenix/oi-ml-shadow.yml`
- tables: `public.option_chain_1m`, `public.oi_ml_shadow_order_intents`
- scorer: smoke deployment uses `OI_ML_SHADOW_SCORER=constant`
- broker access: sidecar forwards backend broker proxy env and reuses the Angel
  quote session during snapshotting
- promotion blocker: market-session Angel FULL quote field completeness still
  must be proven

Use [OI/ML Shadow Sidecar Runbook](oi_ml_shadow_sidecar.md) for sidecar-specific
operations and proof gates.

## OCI VM Assumptions

- Commands run on the OCI VM as the operator user.
- Private IPs, OCIDs, broker identifiers, DB passwords, admin keys, tokens, TOTP,
  PINs, and client codes must be redacted.
- The backend port `8080` is container-local. Use nginx host ports `80` and
  `8443` for host-level health checks.
- `phoenix-oci-postgres` is a VM-local Postgres container with data mounted from
  `/opt/phoenix/pgdata`.
- The current backend image has source-file bind mounts; do not add or remove
  those mounts during documentation or evidence capture.

## Required Access

- SSH/Bastion access to the OCI VM.
- Docker access on the VM, typically through `sudo docker`.
- Read access to `/opt/phoenix`.
- For authenticated admin endpoints, access to the admin key in the approved
  runtime secret path. Do not print the key.

## Required Environment Variable Names

`/opt/phoenix/phoenix-deploy.env` is non-secret runtime configuration. Current
required names observed on the VM:

```text
AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING
CAPITAL_LIMITS_JSON
CLIENT_LOCAL_IP
CLIENT_PUBLIC_IP
CONTROL_PLANE_PG_DB
CONTROL_PLANE_PG_HOST
CONTROL_PLANE_PG_USER
HUB_DEFAULT_BROKER_ACCOUNT_ID
HUB_DEFAULT_TENANT_ID
HUB_ROUTES_JSON
IMAGE_TAG
MAC_ADDRESS
OCIR_NAMESPACE
OCIR_REGION
PHOENIX_CERTS_HOST_PATH
PHOENIX_DOMAIN
PHOENIX_LOG_HOST_PATH
PHOENIX_STATE_HOST_PATH
PROFIT_DAILY_TARGET
RISK_MAX_DAILY_LOSS
```

Runtime secret file names:

```text
/run/secrets/admin_api_key
/run/secrets/auth_token_secret
/run/secrets/control_plane_pg_password
/run/secrets/angel_postback_token
```

## Secret Redaction Rule

Never paste secret values. When inspecting env or logs, redact values:

```bash
docker inspect phoenix-oci-backend \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sed -E 's/=.*$/=<REDACTED>/'
```

## Read-Only Runtime Evidence

```bash
hostname
date
whoami
pwd

docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"

docker inspect phoenix-oci-backend \
  --format '{{json .Config.Image}} {{json .Config.Cmd}} {{json .Config.Entrypoint}} {{json .HostConfig.RestartPolicy}}'

docker inspect phoenix-oci-backend --format '{{json .Mounts}}'
docker inspect phoenix-oci-web --format '{{json .Mounts}}'
docker inspect phoenix-oci-postgres --format '{{json .Mounts}}'
```

Expected success evidence:

- backend and web are `healthy`
- backend image is `phoenix-local-backend:local-1a2cc47`
- web image is `phoenix-local-nginx:local-1a2cc47`
- Postgres is `phoenix-oci-postgres`
- backend command is `python -m app.main`

## Health and Readiness

Backend container checks:

```bash
docker exec phoenix-oci-backend curl -sS http://localhost:8080/health
docker exec phoenix-oci-backend curl -sS http://localhost:8080/ready
docker exec phoenix-oci-backend curl -sS http://localhost:8080/readyz
docker exec phoenix-oci-backend curl -sS http://localhost:8080/health/summary
```

Host/nginx checks:

```bash
curl -sS http://localhost/health
curl -sS http://localhost/readyz
curl -k -sS https://localhost:8443/health
curl -k -sS https://localhost:8443/readyz
```

Expected success evidence:

- HTTP status `200` for all commands above.
- `/health` includes `order_path` equal to `strategy_bridge_order_router`.
- `/health/summary` includes `operating_mode` equal to `HUB_AUTHORITATIVE`.
- `/readyz` includes `ready: true`.

Expected warning:

- `curl http://localhost:8080/...` from the host fails because backend port
  `8080` is not published. Use `docker exec phoenix-oci-backend` for backend
  local checks.
- `/api/health` through nginx is not a health endpoint; it returns the SPA.

## Logs

```bash
docker logs --tail=300 phoenix-oci-backend
docker logs --tail=120 phoenix-oci-web
docker logs --tail=120 phoenix-oci-watchdog

find /opt/phoenix/logs -maxdepth 2 -type f | sort | tail -50
tail -n 200 /opt/phoenix/logs/cron-scheduler.log 2>/dev/null || true
tail -n 200 /opt/phoenix/logs/cert-renewal.log 2>/dev/null || true
```

Expected warnings:

- `phoenix-oci-watchdog` may log backend fail counts and nginx stop/start
  recovery. It actively stops/starts nginx in the verified VM runtime. This is
  current behavior, not observe-only behavior.
- backend logs contain frequent health probes.

Failure handling:

- If backend `/readyz` fails, do not place or cancel orders as a troubleshooting
  step. Capture backend logs, watchdog logs, and Postgres status first.
- If watchdog repeatedly stops nginx, validate backend health from inside the
  backend container before restarting anything.

## Database Inspection

Use the VM-local Postgres container. Do not run destructive SQL.

```bash
docker exec phoenix-oci-postgres \
  psql -U phoenix_app -d phoenix \
  -c "\dt"
```

Safer table list:

```bash
docker exec phoenix-oci-postgres \
  psql -U phoenix_app -d phoenix -qAt \
  -c "select schemaname||'.'||tablename from pg_tables where schemaname not in ('pg_catalog','information_schema') order by 1;"
```

Expected tables include:

- `public.order_submission_outbox`
- `public.position_ownership_ledger`
- `public.internal_position_records`
- `public.strategy_configs`
- `public.broker_credentials`
- `public.kill_switch_state`
- `public.schema_migrations`
- `public.trades`

Do not select broker credential values. Use boolean checks instead:

```bash
docker exec phoenix-oci-postgres \
  psql -U phoenix_app -d phoenix -qAt \
  -c "select broker_account_id, api_key is not null as has_api_key, client_code is not null as has_client_code, updated_at from broker_credentials order by broker_account_id;"
```

## Start, Stop, and Restart

Current scheduled scripts:

- `/opt/phoenix/start-phoenix.sh`
- `/opt/phoenix/stop-phoenix.sh`

Cron evidence observed:

```text
30  3 * * 1-5 /opt/phoenix/start-phoenix.sh >> /opt/phoenix/logs/cron-scheduler.log 2>&1
30 18 * * 0-4 /opt/phoenix/stop-phoenix.sh  >> /opt/phoenix/logs/cron-scheduler.log 2>&1
```

Manual restart of the backend only, after operator approval:

```bash
cd /opt/phoenix/app

CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-deps backend
```

Manual nginx recreate, after backend is healthy:

```bash
cd /opt/phoenix/app

CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-deps nginx
```

Rollback/recovery path:

1. Stop new live entries through the approved control path.
2. Capture `docker ps`, backend `/readyz`, `/health/summary`, backend logs, and
   watchdog logs.
3. Restore the last known-good local backend/nginx image tag and matching
   `/opt/phoenix/phoenix-override.yml`.
4. Recreate backend first.
5. Verify backend `/readyz`.
6. Recreate nginx.
7. Recheck host `/health` and `/readyz`.

Do not run `docker compose down` unless the operator explicitly accepts the full
service interruption.

## Migrations and Preflight

The current VM uses the base Compose `migrator` and `db-preflight` service names,
but the database is local. Run only during an approved deployment window:

```bash
cd /opt/phoenix/app

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

Do not run migrations during documentation review or routine evidence capture.

## Operator Responsibility

The operator owns:

- `/opt/phoenix/phoenix-deploy.env`
- `/opt/phoenix/phoenix-override.yml`
- `/run/secrets/*`
- `/opt/phoenix/pgdata`
- `/opt/phoenix/logs`
- `/opt/phoenix/state`
- cron entries for start/stop/cert/cleanup
- certificate renewal and nginx health
- release evidence capture before any go-live or restart approval

## Known Current Drift

| Drift | Evidence | Risk |
|---|---|---|
| Local images instead of OCIR | `phoenix-local-backend:local-1a2cc47`, `phoenix-local-nginx:local-1a2cc47` | Old OCIR docs do not describe current deploy/restart behavior |
| VM-local Postgres | `CONTROL_PLANE_PG_HOST=phoenix-oci-postgres` | External DB backup/SSL assumptions are not current |
| Source bind mounts | backend mounts selected `/opt/phoenix/app/app/...` files | Container image alone is not the full deployed code |
| Watchdog stops nginx | watchdog command/logs | Nginx availability can change without a manual nginx command |
| Optimizer/reload timers absent | `systemctl status` not found | Do not claim scheduled optimizer/reload is installed |

Any change that removes this drift must be verified from the VM before docs are
updated to describe the new state.
