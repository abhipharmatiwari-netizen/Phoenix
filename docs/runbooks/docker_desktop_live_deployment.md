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
- `CONTROL_PLANE_PG_SSLMODE=disable`
- `HUB_DEFAULT_TENANT_ID=tenant-1`
- `HUB_DEFAULT_BROKER_ACCOUNT_ID=A1`

---

## Example one-line deployment command

Run this from the repo root in PowerShell **only if you are using the bundled host-session helper as a transport step for already-approved runtime values**. Windows SecretStore here is an operator convenience, not the authoritative LIVE secret source:

```powershell
$pw=Read-Host "Enter SecretStore password" -AsSecureString; Unlock-SecretStore -Password $pw; $env:ADMIN_API_KEY_HOST=Get-Secret -Name ADMIN_API_KEY -AsPlainText; $env:DEMO_AUTH_TOKEN_SECRET_HOST=Get-Secret -Name DEMO_AUTH_TOKEN_SECRET -AsPlainText; $env:CONTROL_PLANE_PG_PASSWORD_HOST=Get-Secret -Name CONTROL_PLANE_PG_PASSWORD -AsPlainText; $env:CLIENT_LOCAL_IP=Get-Secret -Name CLIENT_LOCAL_IP -AsPlainText; $env:CLIENT_PUBLIC_IP=Get-Secret -Name CLIENT_PUBLIC_IP -AsPlainText; $env:MAC_ADDRESS=Get-Secret -Name MAC_ADDRESS -AsPlainText; $env:CONTROL_PLANE_PG_HOST="host.docker.internal"; $env:CONTROL_PLANE_PG_PORT="5432"; $env:CONTROL_PLANE_PG_DB="phoenix"; $env:CONTROL_PLANE_PG_USER="phoenix_app"; $env:CONTROL_PLANE_PG_SSLMODE="disable"; $env:HUB_DEFAULT_TENANT_ID="tenant-1"; $env:HUB_DEFAULT_BROKER_ACCOUNT_ID="A1"; docker compose -f .\docker-compose.live.single.yml down --remove-orphans; docker compose -f .\docker-compose.live.single.yml up -d --build --force-recreate; docker compose -f .\docker-compose.live.single.yml ps
```

### Equivalent helper script

You can also use:

```powershell
.\start-docker-secretstore.ps1
```

That helper implements the same bundled Docker/Desktop path.

---

## What the bundled manifest guarantees

The single-file Compose manifest wires the required LIVE settings directly into the backend service definition. The backend container must receive, at minimum:

- `TRADE_MODE=LIVE`
- `ENABLE_MULTI_HUB=true`
- `USE_HUB_ROUTER=true`
- `DISABLE_STREAM_WORKER=false`
- `BROKER_SECRET_BACKEND=postgres`
- `CONTROL_PLANE_BACKEND=postgres`
- `SWEEP_STATE_BACKEND=postgres`
- `APP_RUNTIME_STARTUP_VALIDATE=true`
- `SCHEMA_CHECK_MODE=strict`
- `DASHBOARD_AUTH_DISABLED=false`
- `DISABLE_CONTROL_TOWER_ROUTES=true`
- `ORDER_ROUTER_ENFORCE_IDEMPOTENCY=true`
- `POSITION_OWNERSHIP_ENABLED=true`
- `ENABLE_EOD_EXIT=true`

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
docker compose -f .\docker-compose.live.single.yml exec backend sh -lc "env | egrep '^(TRADE_MODE|ENABLE_MULTI_HUB|USE_HUB_ROUTER|DISABLE_STREAM_WORKER|BROKER_SECRET_BACKEND|CONTROL_PLANE_BACKEND|SWEEP_STATE_BACKEND|APP_RUNTIME_STARTUP_VALIDATE|SCHEMA_CHECK_MODE|DASHBOARD_AUTH_DISABLED|DISABLE_CONTROL_TOWER_ROUTES|ORDER_ROUTER_ENFORCE_IDEMPOTENCY|POSITION_OWNERSHIP_ENABLED|ENABLE_EOD_EXIT)='"
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
