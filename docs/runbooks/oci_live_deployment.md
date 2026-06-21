# Phoenix OCI LIVE Deployment Runbook

Status: current operator runbook for the OCI VM verified on 2026-06-21.

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
- OI/ML shadow retained image source: image inspection; legacy sidecar checkout `/opt/phoenix/oi-ml-shadow-src` may exist but is not current execution evidence
- OI/ML shadow compose: `/opt/phoenix/oi-ml-shadow.yml`
- OI/ML shadow container: absent; retained image and operator Compose remain with restart policy `no`

Latest verified live deployment:

- VM repo checkout: verify with `git -C /opt/phoenix/app rev-parse --short HEAD`
- running Phoenix image tags: verify with
  `docker ps --filter name=phoenix --format '{{.Names}} {{.Image}}'`
- backend image: must be the intended `phoenix-local-backend:local-<git-sha>`
  or approved replacement image for the rollout
- nginx image: must be the intended `phoenix-local-nginx:local-<git-sha>` or
  approved replacement image for the rollout
- live strategy routing: EMA20-only; `AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING=1`
  and non-EMA strategies disabled in `strategy_configs`
- live runtime: `APP_ENV=production`, `TRADE_MODE=LIVE`,
  `REQUIRE_LIVE_TRADE_MODE=true`, `HUB_INSTANCE_NAME=phoenix-oci-prod`, and
  `LEADER_LEASE_ID=phoenix-oci-live`
- liveness: backend `/health` and nginx `/health` return HTTP 200
- readiness: backend-local `/readyz` and nginx `/readyz` return HTTP 200
- public readiness/summary: nginx `/readyz` and `/health/summary` proxy to
  redacted backend endpoints
- health operations screens: nginx `/health/alerts` and `/health/mitigations`
  proxy JSON from the backend; verify the host-mounted nginx template contains
  these routes before recreating nginx
- frontend health rendering: Overview/Safety use authenticated
  `/admin/health/summary` for internal schema, watchdog, and account-count
  fields, then fall back to redacted public `/health/summary`
- BFF hardening: direct `/bff/health/summary`, `/bff/readyz`, and
  `/bff/dashboard/status` are blocked so redaction cannot be bypassed
- Host hardening: `PHOENIX_DOMAIN` and optional `PHOENIX_ALLOWED_HOSTS` are
  passed to the backend; the canonical login host is allowed while malformed
  and unapproved Host headers are rejected before authentication
- database: `phoenix-oci-postgres` is Compose-managed with Docker health
  status `healthy`
- watchdog: `phoenix-oci-watchdog` has no Docker socket or other mounts
- root filesystem: 183G total with 45G available and 76% used at the
  2026-06-13 storage verification; `/health/alerts` includes the
  `disk_headroom_low` rule with a 10G free-space and 90% used-space threshold
- co-tenant controls: Aurelium remains on the host only with compensating
  resource-cap enforcement from
  `/opt/phoenix/scripts/enforce-cotenant-resource-caps.sh`

Non-current for this VM unless a later evidence capture proves otherwise:

- OCIR images
- external OCI Database for PostgreSQL
- Docker Desktop deployment
- Cloud Run/GCP deployment
- Firestore or BigQuery as operational authority
- optimizer and backend-reload systemd timers

## OI/ML Shadow Sidecar

The OI/ML CE seller sidecar is separate from the live backend/nginx stack and is
currently dormant. It publishes no host ports and has no live order authority.
Its historical Postgres rows, retained image, and logs remain available for
review, but no snapshots or intents are currently generated.

Current sidecar evidence is the absence of a `phoenix-oi-ml-shadow` container,
the retained image, and the operator Compose file. Verify it with `docker ps -a`,
`docker image inspect`, and the operator Compose file without printing env values.

- retained image: `phoenix-oi-ml-shadow:oi-ml-shadow-e5e13bd`
- compose: `/opt/phoenix/oi-ml-shadow.yml`
- persistent dormancy: `restart: "no"`, `OI_ML_SHADOW_ENABLED=false`, and
  `OI_SNAPSHOTTER_ENABLED=false`
- tables: `public.option_chain_1m`, `public.oi_ml_shadow_order_intents`,
  `public.option_chain_validation_reports`
- scorer: retained configuration is `missing`; promotable shadow requires
  validated LightGBM artifacts and a passed model-validation report; `constant`
  is smoke-only and requires an explicit override
- broker access when explicitly enabled: sidecar forwards backend broker proxy
  env and reuses the Angel quote session during snapshotting
- health visibility: backend currently sets
  `OI_ML_SHADOW_HEALTH_ENABLED=false` and reports OI/ML status `disabled`.
  When explicitly re-enabled, sidecar readiness/data-quality checks use
  `python -m app.strategies.oi_ml.shadow_health` and require the sidecar
  compose to provide the VM-local non-secret `CONTROL_PLANE_DB_DSN`.
- NSE validation: the sidecar falls back to NSE live-derivatives rows when the
  classic NSE option-chain JSON endpoint returns an empty payload; this fallback
  validates OI/volume/LTP only and records skipped IV/bid/ask fields in report
  metadata
- IV handling: missing Angel IV is enriched at read time only when fresh
  exact-contract `nse_web` rows contain IV; the live-derivatives fallback does
  not supply IV, so it is not promotion evidence for IV enrichment
- reactivation/promotion blocker: explicit operator review, market-session and
  market-calendar gating, hard-field completeness, fresh source
  timestamps, IV/Greeks, latest validation not `ERROR`, terminal virtual
  lifecycle/PnL, acceptable metrics, and 10 clean sessions still must be proven

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
PHOENIX_ALLOWED_HOSTS
PHOENIX_LOG_HOST_PATH
PHOENIX_STATE_HOST_PATH
PROFIT_DAILY_TARGET
RISK_MAX_DAILY_LOSS
```

Current live strategy posture as of 2026-06-20: set
`AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING=1` for EMA20-only routing. Do not
restore older multi-strategy values unless selector mappings, instrument
allow-lists, `strategy_configs`, tests, and release evidence are updated
together.

Runtime secret file names:

```text
/run/secrets/admin_api_key
/run/secrets/auth_token_secret
/run/secrets/control_plane_pg_password
/run/secrets/angel_postback_token
/run/secrets/admin_kill_switch_override
```

`/run/secrets/admin_kill_switch_override` is file-only. The entrypoint
must not export it into the backend process environment. The fetch
script owns this file to the backend app user and uses mode `0400` so
the non-root backend can read it without making it world-readable.

Current live permission model:

- shared runtime files such as `admin_api_key` and
  `control_plane_pg_password` are owned by UID 100 with root group read and mode
  `0440`;
- backend-only runtime files use UID 100/GID 101 and mode `0400`;
- `scripts/validate-live-secret-perms.sh` must pass before a LIVE deployment is
  treated as hardened.
- `scripts/ops/check_env_secret_material.sh` must pass for
  `/opt/phoenix/phoenix-deploy.env` and any retained backups before a LIVE
  deployment is treated as hardened.

Host-header boundary:

- set `PHOENIX_DOMAIN` to the public Phoenix hostname;
- set `PHOENIX_ALLOWED_HOSTS` when additional internal hostnames are required,
  using a comma-separated allow-list;
- malformed `Host` headers containing path, query, fragment, whitespace, or
  encoded path material must return HTTP 400 before admin or BFF auth logic
  runs.

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

- backend and web are running; after the 2026-05-21 liveness-healthcheck patch
  they remain Docker-healthy when `/health` is 200 even if `/readyz` is 503
- backend image tag matches the intended deployment SHA or approved replacement
  image for the rollout
- web image tag matches the intended deployment SHA or approved replacement
  image for the rollout
- `/opt/phoenix/phoenix-override.yml` must also use `/health` for nginx
  Docker health; a VM-local override that still checks `/readyz` will keep the
  web container unhealthy during an intentional trading-readiness halt.
- Postgres is `phoenix-oci-postgres`
- Postgres reports Docker health status `healthy`
- watchdog inspect reports no mounts
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
curl -k -sS https://localhost:8443/nginx-health
curl -sS http://localhost/health
curl -sS http://localhost/readyz
curl -sS http://localhost/health/summary
curl -sS http://localhost/health/alerts
curl -sS http://localhost/health/mitigations
curl -k -sS https://localhost:8443/health
curl -k -sS https://localhost:8443/readyz
curl -k -sS https://localhost:8443/health/summary
curl -k -sS https://localhost:8443/health/alerts
curl -k -sS https://localhost:8443/health/mitigations
```

Expected normal trading-readiness evidence:

- `/nginx-health` returns `ok`; this is nginx-only liveness and does not require
  the backend to be running.
- HTTP status `200` for all commands above.
- `/health` includes `order_path` equal to `strategy_bridge_order_router`.
- `/health/summary` includes `operating_mode` equal to `HUB_AUTHORITATIVE`.
- `/readyz` includes `ready: true`.
- Host/nginx `/readyz` and `/health/summary` return only redacted public
  readiness fields. Use backend-local `/readyz` and `/health/summary` for full
  internal diagnostics.
- Host/nginx `/health/alerts` and `/health/mitigations` must return
  `application/json`; if either returns SPA HTML, patch
  `/opt/phoenix/nginx-ssl-prerendered.conf.template` and recreate nginx.
- Authenticated `/admin/health/summary` should return detailed internal fields
  such as `schema_status`, `tracked_account_count`, and `watchdog_running`.
- If Overview or Safety shows Schema Status, Tracked Accounts, or Watchdog as
  `Unknown`, first compare against authenticated `/admin/health/summary`. Public
  redaction alone should not be treated as a component failure.
- Public `/bff/health/summary`, `/bff/readyz`, and `/bff/dashboard/status`
  should return 404 to prevent bypassing redaction.
- The frontend Overview page must continue to render if only the public
  redacted `/health/summary` is available; missing internal-only fields should
  appear as fallback values, not as a runtime error.

Risk-halt or degraded evidence:

- `/health` should still return HTTP `200` so the dashboard remains reachable.
- `/readyz` may return HTTP `503` with `ready: false` when the kill switch,
  position authority, stale sync, or another trading-readiness gate is active.
- Treat non-200 `/readyz` as a block on new live entries. Do not clear the gate
  just to make Docker health green.
- If logs show `mark.unavailable` or PnL snapshots with
  `freshness_source=broker_sync_stale_mark`, treat strategy/account PnL as
  incomplete until a live LTP-backed broker sync arrives. Do not use stale-mark
  PnL to clear risk gates or justify new live entries.

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

- `phoenix-oci-watchdog` should poll backend `/health` and log fail/recovery
  counts without stopping or starting nginx. If logs show nginx stop/start
  actions, the VM is running stale watchdog wiring or an override and must be
  redeployed before relying on dashboard availability during readiness halts.
- backend logs contain frequent health probes.

Failure handling:

- If backend `/readyz` fails, do not place or cancel orders as a troubleshooting
  step. Capture backend logs, watchdog logs, and Postgres status first.
- If evidence shows the watchdog stopping nginx, treat that as stale VM wiring.
  Validate backend health from inside the backend container and follow the OCI
  runtime hardening runbook before relying on dashboard availability.

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
- `public.strategy_config_candidates`
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
30 18 * * 0-5 /opt/phoenix/stop-phoenix.sh  >> /opt/phoenix/logs/cron-scheduler.log 2>&1
```

Run the migration and database preflight steps below before any backend/nginx
recreate for a release that changes `migrations/`, schema guard requirements, or
live startup validation. Manual restart of the backend only, after operator
approval:

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
| Local images instead of OCIR | `phoenix-local-backend:local-<git-sha>` and `phoenix-local-nginx:local-<git-sha>` verified from `docker ps` during each rollout | Old OCIR docs do not describe current deploy/restart behavior |
| VM-local Postgres | `CONTROL_PLANE_PG_HOST=phoenix-oci-postgres`; container is Compose-managed and healthy | External DB backup/SSL assumptions are not current |
| Source bind mounts | backend mounts selected `/opt/phoenix/app/app/...` files | Container image alone is not the full deployed code |
| Watchdog must remain observe-only | watchdog inspect should report no mounts | Docker socket mounts or nginx stop/start logs indicate stale VM wiring or override drift |
| Optimizer/reload timers absent | `systemctl status` not found | Do not claim scheduled optimizer/reload is installed |

Any change that removes this drift must be verified from the VM before docs are
updated to describe the new state.
