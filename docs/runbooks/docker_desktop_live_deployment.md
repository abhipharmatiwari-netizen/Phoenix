# Phoenix v9 Docker Desktop LIVE Deployment

> **Status:** Bundled Docker/Desktop implementation runbook for the current recommended automated LIVE runtime.

This runbook describes the Docker Desktop / Windows path bundled with this document set.

It uses:

- [`docker-compose.live.single.yml`](../../docker-compose.live.single.yml)
- Postgres as the authoritative operational store
- Postgres `broker_credentials` as the broker-secret path used by the bundled manifest
- runtime-injected platform secrets for values such as `ADMIN_API_KEY`, auth token secret, and database password

The older multi-file Compose path (`docker-compose.live.yml` + `docker-compose.postgres.override.yml` + `.docker-live.env`) is not a bundled go-live path unless you separately audit it and prove that the backend container resolves the full automated LIVE contract.

---

## Current recommended automated LIVE contract for this runbook

This runbook is correct only for the following deployment model:

- `TRADE_MODE=LIVE`
- `ENABLE_MULTI_HUB=true`
- `USE_HUB_ROUTER=true`
- `DISABLE_STREAM_WORKER=false`
- stream worker enabled for broker market data, ticks, bars, indicators, live marks, and strategy signal generation
- hub/router/lifecycle/account-runner path authoritative for order submission, idempotency, ownership, lifecycle, broker sync, and reconciliation
- Postgres authoritative for operational state
- `BROKER_SECRET_BACKEND=postgres` in the bundled manifest

### Secret boundary

`ARCHITECTURE.md` requires LIVE secrets to come from **Secret Manager or Postgres**. Short-lived injected environment variables may transport those values into the runtime. Repo env files are not approved secret sources.

This aligned bundle still includes a Windows PowerShell helper for convenience. Treat that helper as a host-side export step only, not as a change to the architecture's source-of-truth rule for secrets.

---

## Files used by this runbook

- [`../../docker-compose.live.single.yml`](../../docker-compose.live.single.yml)
- [`../../start-docker-secretstore.ps1`](../../start-docker-secretstore.ps1)
- [`update_broker_credentials.md`](update_broker_credentials.md)
- [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)

---

## Prerequisites

Before you start the bundled LIVE stack, all of the following must already be true:

- Docker Desktop is running.
- PostgreSQL is reachable from Docker at `host.docker.internal:5432`, or you have exported alternate Postgres host/port values into the PowerShell session.
- The Postgres database already contains the required Phoenix control-plane tables.
- The target tenant exists.
- The target broker account exists.
- The target subscription / strategy config rows exist.
- The target `broker_credentials` row exists for the chosen `broker_account_id`.
- Port `80` is free on the host.
- The runtime can obtain `ADMIN_API_KEY`, `DEMO_AUTH_TOKEN_SECRET`, `CONTROL_PLANE_PG_PASSWORD`, `CLIENT_LOCAL_IP`, `CLIENT_PUBLIC_IP`, and `MAC_ADDRESS` from your approved LIVE secret process.

### If you use the bundled PowerShell helper

The helper script expects the following Windows modules and secret names:

- PowerShell `SecretManagement` and `SecretStore` modules installed
- `ADMIN_API_KEY`
- `DEMO_AUTH_TOKEN_SECRET`
- `CONTROL_PLANE_PG_PASSWORD`
- `CLIENT_LOCAL_IP`
- `CLIENT_PUBLIC_IP`
- `MAC_ADDRESS`

Use that helper only when it is acting as your operator-side export step for values managed under the approved LIVE secret process.

### Required PowerShell runtime values

The bundled example command sets these explicitly if you do not override them:

- `CONTROL_PLANE_PG_HOST=host.docker.internal`
- `CONTROL_PLANE_PG_PORT=5432`
- `CONTROL_PLANE_PG_DB=phoenix`
- `CONTROL_PLANE_PG_USER=phoenix_app`
- `CONTROL_PLANE_PG_SSLMODE=prefer`
- `CAPITAL_LIMITS_JSON={"tenant-1:A1": {"max_notional_per_order": 500000, "max_gross_exposure": 1000000}}`
- `HUB_DEFAULT_TENANT_ID=tenant-1`
- `HUB_DEFAULT_BROKER_ACCOUNT_ID=A1`

---

## Deployment command

Run this from the repo root in PowerShell. Windows SecretStore here is an
operator convenience, not the authoritative LIVE secret source:

```powershell
.\start-docker-secretstore.ps1
```

The helper implements the bundled Docker/Desktop path and derives an
account-specific `CAPITAL_LIMITS_JSON` baseline if no override is present.
It writes Docker Compose secret files under `$env:TEMP\phx-secrets` and
intentionally keeps them there while the stack is running; Compose local secrets
are bind mounts, so deleting those files breaks container restarts. Remove that
directory only after `docker compose down`.

Do not use raw `docker compose up` directly unless the current PowerShell
session has already exported all required non-secret env vars and
`PHX_SECRET_DIR` points at existing `admin_api_key`, `demo_auth_token_secret`,
and `control_plane_pg_password` files.

---

## What the bundled manifest guarantees

The single-file Compose manifest wires the required LIVE settings directly into the backend service definition. The backend container must receive, at minimum:

- `TRADE_MODE=LIVE`
- `REQUIRE_LIVE_TRADE_MODE=true` — startup validator hard-fails if `TRADE_MODE != LIVE`; prevents accidental SHADOW/PAPER deployment of this manifest
- `ENABLE_MULTI_HUB=true`
- `USE_HUB_ROUTER=true`
- `DISABLE_STREAM_WORKER=false`
- `BROKER_SECRET_BACKEND=postgres`
- `CONTROL_PLANE_BACKEND=postgres`
- `SWEEP_STATE_BACKEND=postgres`
- `APP_RUNTIME_STARTUP_VALIDATE=true`
- `SCHEMA_CHECK_MODE=strict`
- `BROKER_SCHEMA_CHECK_MODE=strict` — Angel One API responses validated at every balance sync; malformed responses rejected at the integration boundary
- `DASHBOARD_AUTH_DISABLED=false`
- `DISABLE_CONTROL_TOWER_ROUTES=false`
- `ORDER_ROUTER_ENFORCE_IDEMPOTENCY=true`
- `POSITION_OWNERSHIP_ENABLED=true`
- `ENABLE_EOD_EXIT=true`
- `RISK_STATE_PATH=/app/state/risk_positions.json` — risk restart-helper persisted to the `/app/state` volume, separate from the log volume

---

## Verification after startup

### 1. Container health

```powershell
docker compose -f .\docker-compose.live.single.yml ps
```

Expected state:

- `backend` is `Up (healthy)`
- `nginx` is `Up`
- `db-preflight` exited successfully

### 2. HTTP health

```powershell
curl.exe http://localhost/health
curl.exe http://localhost/health/summary
```

### 3. Effective backend environment

This proves the backend container, not just the host shell, resolved the required automated LIVE tuple:

```powershell
docker compose -f .\docker-compose.live.single.yml exec backend sh -lc "env | egrep '^(TRADE_MODE|REQUIRE_LIVE_TRADE_MODE|ENABLE_MULTI_HUB|USE_HUB_ROUTER|DISABLE_STREAM_WORKER|BROKER_SECRET_BACKEND|CONTROL_PLANE_BACKEND|SWEEP_STATE_BACKEND|APP_RUNTIME_STARTUP_VALIDATE|SCHEMA_CHECK_MODE|BROKER_SCHEMA_CHECK_MODE|DASHBOARD_AUTH_DISABLED|DISABLE_CONTROL_TOWER_ROUTES|ORDER_ROUTER_ENFORCE_IDEMPOTENCY|POSITION_OWNERSHIP_ENABLED|ENABLE_EOD_EXIT|RISK_STATE_PATH)='"
```

### 4. Stream-worker startup evidence

For automated LIVE, confirm the logs show the market-data/strategy plane starting successfully.

```powershell
docker compose -f .\docker-compose.live.single.yml logs --tail 200 backend
```

Look for evidence of broker login, universe build, websocket startup, indicator seeding, or strategy runtime startup. If the runtime behaves as operator/control-plane only, automated LIVE readiness has not been proven.

### 5. Rendered manifest evidence

Capture the resolved Compose model as release evidence:

```powershell
docker compose -f .\docker-compose.live.single.yml config > .\compose.rendered.live.yml
```

Keep that file with the release evidence for the deployment.

### 6. Clean promotion artifact

Build the promotion artifact from the git-tracked source tree, not from a whole working-directory snapshot:

```powershell
python .\scripts\build_release_artifact.py --output .\release\phoenix-live-source.zip
```

That artifact intentionally excludes local clutter such as `logs/`, `__pycache__/`, `.pytest_cache/`, `.venv/`, test trees, and temp output roots. Keep the generated zip with the rendered manifest as release evidence.

---

## Expected startup log messages

The following WARNING-level messages can appear on a clean host-local Docker/Desktop startup and are **expected behavior**, not incidents. Do not open an incident for these alone.

| Message | Why expected |
|---|---|
| `LIVE mode policy gates enforced hardened defaults for: {...}` | Confirms LIVE-mode flags were auto-promoted; informational |
| `startup.ssl_warning: LIVE_PG_SSL_SKIP_CHECK=true` | Expected only for host-local Docker deployments where Postgres does not have SSL enabled; harmless when Postgres is on `host.docker.internal` with no external exposure |
| `illegal transition blocked ... from_state=RECONCILING ... escalating to DEGRADED` | Stale position records from expired prior-session option contracts are safely escalated to DEGRADED; not a live position problem |

Messages containing `strategy.unroutable` or `strategy.unroutable_selector_excluded`
are **not expected** after a clean bundled LIVE startup. They mean a strategy is
attached in runtime but has no hub route, and the route/config drift must be fixed
before using the strategy with real money.

The one message that is **NOT** expected after a clean startup is `BROKER_SCHEMA_VIOLATION` at CRITICAL level repeating on a timer. That indicates a persistent broker balance schema mismatch and requires investigation.

---

## Common failures and exact checks

### Backend fails during startup

Most likely causes:

- Postgres unreachable
- missing `broker_credentials` row for the selected `broker_account_id`
- missing or invalid strategy routing rows
- invalid `HUB_ROUTES_JSON`
- missing runtime secret values
- LIVE startup gate failure
- stream worker cannot establish broker market-data session

Check:

```powershell
docker compose -f .\docker-compose.live.single.yml logs --tail 200 backend
```

### `db-preflight` exits with an error

This usually means the control-plane database is missing required tables or the selected broker account does not yet have a `broker_credentials` row.

Check:

```powershell
docker compose -f .\docker-compose.live.single.yml logs db-preflight
```

Then fix the control-plane data before restarting.

### nginx is up but the UI does not load

Check the nginx logs:

```powershell
docker compose -f .\docker-compose.live.single.yml logs --tail 100 nginx
```

If the backend became healthy after nginx started, recreate nginx:

```powershell
docker compose -f .\docker-compose.live.single.yml up -d --force-recreate nginx
```

### Port 80 already in use

Free the host port or change the published nginx port in the manifest before redeploying.

### Automated LIVE starts but there are no fresh marks or strategy signals

Check whether the backend effective environment or logs show an accidental stream-disabled state, broker websocket failure, or instrument-universe startup failure. Automated LIVE is not healthy when broker sync is running but live marks, bars, and indicators are stale.

---

## Restart and stop

### Restart only the backend

Use this after control-plane data changes such as broker credential rotation:

```powershell
docker compose -f .\docker-compose.live.single.yml restart backend
```

### Rebuild and redeploy

Use this after code or manifest changes:

```powershell
docker compose -f .\docker-compose.live.single.yml up -d --build --force-recreate
```

### Stop the stack

```powershell
docker compose -f .\docker-compose.live.single.yml down --remove-orphans
```

---

## Release evidence checklist

For each deployment, capture all of the following:

- rendered Compose file from `docker compose config`
- `docker compose ps` output
- backend effective LIVE env output
- `/health/summary` output
- backend startup log excerpt showing startup validation succeeded
- backend log excerpt showing stream-worker market-data/strategy startup for automated LIVE

A deployment is not ready for automated LIVE without this evidence.
