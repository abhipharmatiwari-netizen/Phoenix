# Phoenix v9 Docker Desktop LIVE Deployment

> **Status:** ACTIVE LOCAL RECOVERY RUNTIME WHILE OCI VM IS UNAVAILABLE.
>
> This runbook is the current operating reference for the Windows Docker Desktop
> recovery deployment connected to Windows PostgreSQL 18 `phoenix` and exposed
> through the Vultr sidecar/proxy path. It is not a permanent replacement for a
> dedicated production host.

> **OCI restoration?** Use [oci_live_deployment.md](oci_live_deployment.md) and
> [OCI VM Runtime Evidence](../OCI_VM_RUNTIME.md). The verified OCI VM used local
> images and VM-local Postgres; do not assume OCIR or external Postgres from old
> Docker/Desktop wording.

## Purpose

This runbook describes the Docker Desktop / Windows path bundled with this repo.

It uses:

- [`docker-compose.live.single.yml`](../../docker-compose.live.single.yml)
- Postgres as the authoritative operational store
- Postgres `broker_credentials` as the broker-secret and broker-network-identity
  path used by the bundled manifest
- runtime-injected platform secrets for values such as `ADMIN_API_KEY`, auth token secret, and database password

The older multi-file Compose path (`docker-compose.live.yml` + `docker-compose.postgres.override.yml`) is obsolete in this repo. Those files are not present and must not be used as current LIVE guidance.

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

## 2026-06-28 Local Replica Validation

This local Docker Desktop stack was rebuilt from the current repo and connected
to the Windows PostgreSQL 18 database `phoenix` on `host.docker.internal:5432`.
This is the active local OCI-VM replica while the OCI VM is unavailable.

Validated state:

- live stack containers: `phoenix-v9-web` healthy on `127.0.0.1:80`, and
  `phoenix-v9-backend` healthy;
- public Vultr access is now handled by sidecar container
  `phoenix-v9-vultr-tunnel`, which waits for nginx liveness
  (`/nginx-health`) and owns the reverse SSH tunnel to `65.20.69.50`;
- effective backend tuple: `APP_ENV=production`, `TRADE_MODE=LIVE`,
  `REQUIRE_LIVE_TRADE_MODE=true`, `CONTROL_PLANE_PG_DB=phoenix`,
  `CONTROL_PLANE_PG_USER=phoenix_app`, `BROKER_SECRET_BACKEND=postgres`,
  `SCHEMA_CHECK_MODE=strict`, `BROKER_SCHEMA_CHECK_MODE=strict`,
  `LEADER_LEASE_BACKEND=postgres`, and `LEADER_LEASE_ID=phoenix-local-live`;
- public `/health` is the login-path liveness check; public `/readyz` is the
  trading-readiness check and can return HTTP 503 during an intentional risk
  halt;
- Postgres migrations were current with 27 `schema_migrations` records;
- `phoenix_app` successfully counted every consolidated table: 36 `public`
  base tables plus 6 archived `legacy_phoneix` tables;
- the local `phoneix` typo database remains preserved as a source archive, and
  its rows are consolidated under `phoenix.legacy_phoneix`;
- one broker credential row exists in Postgres for the live account; no broker
  credential values were printed or copied into documentation;
- `broker_accounts` has `tenant-1/A1` enabled with `trading_mode=LIVE`;
- `admin@phoenix.com` has explicit local dashboard entitlements for
  `tenant-1/A1`, so the UI can show the live tenant/account instead of the
  "No tenant entitlements" banner;
- `strategy_configs` is EMA20-only: `ema20_strategy=true`; all other listed
  strategies are disabled;
- `internal_position_records` has zero active/non-terminal rows and
  `position_ownership_ledger` has zero rows;
- one stale expired NIFTY `RECOVERY_PENDING` record was cleared through
  `POST /admin/state/clear-position-record` with `force=false` after the
  recovery endpoint reported broker-flat evidence; the clear was audited;
- runtime table ownership for `bar_regime`,
  `position_trailing_lock_inflight`, and `position_trailing_lock_state` was
  repaired from `postgres` to `phoenix_app` so the live app can complete its
  idempotent runtime table checks without owner warnings.

Final clean-start log evidence after the owner repair:

- schema guard passed for required Postgres tables and indexes;
- expired-position cleanup found a clean DB;
- zero non-terminal position records were restored from Postgres;
- outbox recovery scanned zero unresolved active records;
- `startup.runtime_ready` set the readiness latch with `recovery_status=ok`.

The older PAPER/local validation stack that previously served
`127.0.0.1:8080` and `127.0.0.1:18080` was removed after this validation.
The LIVE-capable local replica is the only Phoenix stack expected to remain
running, on `127.0.0.1:80`.

Temporary public access for this local replica is handled by the Vultr reverse
proxy runbook at [Vultr Reverse Proxy For Local Phoenix](vultr_reverse_proxy.md).
That path keeps Phoenix and Postgres local, forwards only through the
`phoenix-v9-vultr-tunnel` Docker sidecar to Vultr localhost, and uses HTTPS on
`app.phoenixtechnosolutions.in` for UI login and live operations through the
public endpoint.

## Scope

This runbook covers only `docker-compose.live.single.yml` launched from a
Windows PowerShell session, including the `vultr-tunnel` sidecar. It does not
cover OCI, Cloud Run, legacy multi-file Compose profiles, Firestore-backed
authority, or env-file secret sourcing.

### Secret boundary

`ARCHITECTURE.md` requires LIVE secrets to come from an approved platform secret store; broker credentials may use Postgres. Short-lived injected environment variables and Docker secret files may transport those values into the runtime. Repo env files are not approved secret sources.

This aligned bundle still includes a Windows PowerShell helper for convenience. Treat that helper as a host-side export step only, not as a change to the architecture's source-of-truth rule for secrets.

---

## Files used by this runbook

- [`../../docker-compose.live.single.yml`](../../docker-compose.live.single.yml)
- [`../../docker/vultr-tunnel/Dockerfile`](../../docker/vultr-tunnel/Dockerfile)
- [`../../scripts/ops/vultr_reverse_tunnel_entrypoint.sh`](../../scripts/ops/vultr_reverse_tunnel_entrypoint.sh)
- [`../../start-docker-secretstore.ps1`](../../start-docker-secretstore.ps1)
- [`update_broker_credentials.md`](update_broker_credentials.md)
- [`vultr_reverse_proxy.md`](vultr_reverse_proxy.md)
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
- If public Vultr access is required, the SSH key exists at
  `C:\Users\abhis\.ssh\phoenix_vultr_proxy_workspace_ed25519`, or
  `VULTR_REVERSE_TUNNEL_SSH_KEY` points to the active key path.
- The runtime can obtain bootstrap-only values `ADMIN_API_KEY`,
  `DEMO_AUTH_TOKEN_SECRET`, `CONTROL_PLANE_PG_PASSWORD`,
  `ANGEL_POSTBACK_TOKEN`, and `ADMIN_KILL_SWITCH_OVERRIDE` from your approved
  LIVE secret process. These are not fetched from Postgres: the database
  password is required before Postgres can be queried, and admin / kill-switch
  secrets intentionally stay file-mounted instead of database-readable.
- The selected `broker_accounts` row contains account-specific
  `meta.capital_limits` and, when managed per-account, `meta.risk.max_daily_loss`
  or `meta.risk_max_daily_loss`.
- The selected `broker_credentials` row contains non-empty `api_key`,
  `client_code`, `pin`, and `totp_secret`, plus `client_local_ip`,
  `client_public_ip`, and `mac_address`. Broker login secrets and broker
  network identity are fetched from Postgres; they must not be supplied through
  `ANGEL_*` environment variables for this LIVE stack.

### If you use the bundled PowerShell helper

The helper script expects the following Windows modules and secret names:

- PowerShell `SecretManagement` and `SecretStore` modules installed
- `ADMIN_API_KEY`
- `DEMO_AUTH_TOKEN_SECRET`
- `CONTROL_PLANE_PG_PASSWORD`
- `ANGEL_POSTBACK_TOKEN` (§126 — required for Angel broker postback authentication; without it all Angel postbacks return HTTP 401 and the lifecycle service misses fill events)
- `ADMIN_KILL_SWITCH_OVERRIDE` (file-mounted only; never export or log it)

The helper also connects to Postgres before `docker compose up` and verifies /
exports:

- `CAPITAL_LIMITS_JSON` from `broker_accounts.meta.capital_limits` or
  `broker_accounts.meta.capital_limits_json`. The payload must include the
  selected account key, such as `tenant-1:A1` or `A1`; generic-only `default`
  limits are rejected for LIVE.
- `RISK_MAX_DAILY_LOSS` from `broker_accounts.meta.risk.max_daily_loss`,
  `broker_accounts.meta.risk_max_daily_loss`, or an explicit host env override.
- `CLIENT_LOCAL_IP`, `CLIENT_PUBLIC_IP`, and `MAC_ADDRESS` from
  `broker_credentials`.
- Non-empty Postgres broker credential fields required for Angel login:
  `api_key`, `client_code`, `pin`, and `totp_secret`. Values are never printed.

Use that helper only when it is acting as your operator-side export step for values managed under the approved LIVE secret process.

### Required PowerShell runtime values

The bundled example command sets these explicitly if you do not override them:

- `CONTROL_PLANE_PG_HOST=host.docker.internal`
- `CONTROL_PLANE_PG_PORT=5432`
- `CONTROL_PLANE_PG_DB=phoenix`
- `CONTROL_PLANE_PG_USER=phoenix_app`
- `CONTROL_PLANE_PG_SSLMODE=require`

  > **SSL exception for local Docker Desktop only**: If your Postgres instance on
  > `host.docker.internal` does not have SSL configured (the default for a bare
  > local install), you may set `LIVE_PG_SSL_SKIP_CHECK=true` in your PowerShell
  > session **before** running the start script. This bypasses the SSL enforcement
  > check and emits audited info-level startup telemetry for recognized local
  > Docker Postgres hosts. This exception is valid **only** when Postgres is on
  > the local machine with no external network exposure. Unknown hosts still emit
  > warning-level telemetry, and cloud deployments hard-abort if
  > `LIVE_PG_SSL_SKIP_CHECK=true` is detected. Section 105
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

The helper implements the bundled Docker/Desktop path and loads account-specific
capital/risk/network values from Postgres for the selected tenant/account.
It also preflights the selected `broker_credentials` row so blank Postgres
broker-secret columns fail deployment before any container is recreated.
It writes Docker Compose secret files under `$env:TEMP\phx-secrets` and
intentionally keeps them there while the stack is running; Compose local secrets
are bind mounts, so deleting those files breaks container restarts. Remove that
directory only after `docker compose down`.

Do not use raw `docker compose up` directly unless the current PowerShell
session has already exported all required non-secret env vars and
`PHX_SECRET_DIR` points at existing `admin_api_key`, `demo_auth_token_secret`,
`control_plane_pg_password`, `angel_postback_token`, and
`admin_kill_switch_override` files.

`admin_kill_switch_override` is file-only. The entrypoint must not
export it into the backend process environment.

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
- `ORDER_ROUTER_ENFORCE_GLOBAL_KILL_SWITCH=true`
- `POSITION_OWNERSHIP_ENABLED=true`
- `ENABLE_EOD_EXIT=true`
- `RISK_STATE_PATH=/app/state/risk_positions.json` — risk restart-helper persisted to the `/app/state` volume, separate from the log volume

### §133 — State and log volume paths

> **Required for production:** set `PHOENIX_STATE_HOST_PATH` and
> `PHOENIX_LOG_HOST_PATH` to paths **outside the repo root** to prevent
> pytest runs on the same machine from overwriting production state files
> between LIVE sessions.

```powershell
# Recommended — set before running start-docker-secretstore.ps1:
$env:PHOENIX_STATE_HOST_PATH = "C:\ProgramData\phoenix\state"
$env:PHOENIX_LOG_HOST_PATH   = "C:\ProgramData\phoenix\logs"
```

If these are not set, the defaults (`./state` and `./logs`) inside the repo
root are used — which is shared with pytest's write paths.  The LIVE startup
emits a `startup.state_path_inside_repo` WARNING when this is detected.

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
docker compose -f .\docker-compose.live.single.yml exec backend sh -lc "env | egrep '^(TRADE_MODE|REQUIRE_LIVE_TRADE_MODE|ENABLE_MULTI_HUB|USE_HUB_ROUTER|DISABLE_STREAM_WORKER|BROKER_SECRET_BACKEND|CONTROL_PLANE_BACKEND|SWEEP_STATE_BACKEND|APP_RUNTIME_STARTUP_VALIDATE|SCHEMA_CHECK_MODE|BROKER_SCHEMA_CHECK_MODE|DASHBOARD_AUTH_DISABLED|DISABLE_CONTROL_TOWER_ROUTES|ORDER_ROUTER_ENFORCE_IDEMPOTENCY|ORDER_ROUTER_ENFORCE_GLOBAL_KILL_SWITCH|POSITION_OWNERSHIP_ENABLED|ENABLE_EOD_EXIT|RISK_STATE_PATH|ANGEL_POSTBACK_AUTH_MODE)='"
```

`ANGEL_POSTBACK_AUTH_MODE` must resolve to `direct_broker` in LIVE. If it is absent or set to `disabled`, all Angel broker postbacks return HTTP 401 and the lifecycle service will not receive fill events from broker-initiated callbacks (§126).

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

## 2026-06-29 incident regression checks

After any redeploy that can affect strategy exits, risk checks, broker sync, or
the router, run these checks before considering automated LIVE recovered:

- Tail backend logs through at least one scheduler/order-sync cycle and, during
  market hours, through the next EOD boundary for the active underlyings.
- For EMA20, confirm a full-exit ACK is not followed by
  `EMA20 adopted synced position` for the same label while a pending full exit
  is still active. The expected LIVE behavior is
  `EMA20 skipped synced position adoption` until broker state is fresh.
- Confirm repeated EOD/SL/trail attempts for the same date, underlying, label,
  and reason share one idempotency key. A duplicate attempt may log
  `ORDER_IDEMPOTENCY_SUPPRESSED`; it must not produce another broker
  `ORDER_PLACED` for the same full exit.
- Watch for `broker_sync_stale_mark`, `broker_sync_mark_unavailable`,
  `mark.unavailable`, or `PnL snapshot unavailable`. New entries must be
  blocked fail-closed while total PnL is unavailable; reducing exits may still
  proceed through the router safety checks.
- If `kill_switch.trip` appears, capture `reasons`, realized/unrealized/total
  PnL, drawdown fields, open labels, and `evaluation_source` before clearing.
  Do not clear until broker-side positions and open orders are verified.

---

## Expected startup log messages

The following WARNING-level messages can appear on a clean host-local Docker/Desktop startup and are **expected behavior**, not incidents. Do not open an incident for these alone.

| Message | Why expected |
|---|---|
| `LIVE mode policy gates enforced hardened defaults for: {...}` | Confirms LIVE-mode flags were auto-promoted; informational |
| `startup.ssl_warning: LIVE_PG_SSL_SKIP_CHECK=true` | Expected only for host-local Docker deployments where Postgres does not have SSL enabled; harmless when Postgres is on `host.docker.internal` with no external exposure |
| `WebSocket proxy configured: <host>:<port>` | Expected on OCI/cloud deployments when `ANGEL_HTTPS_PROXY` is set; confirms WebSocket traffic will tunnel through the proxy |
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

If fresh marks are missing but broker positions still exist, total PnL should be
reported unavailable rather than realized-only. Treat any
`broker_sync_stale_mark` or `mark.unavailable` event as an entry-blocking
condition until market data or broker marks recover.

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

### Rollback / recovery

If a rebuilt backend is unhealthy, restore the last known-good git revision or image, rerun the same `start-docker-secretstore.ps1` deployment, and repeat the verification steps. Do not resume automated LIVE until `/readyz`, stream-worker evidence, balance sync readiness, and release evidence pass again.

The operator owns the decision to hold, roll back, or keep the stack stopped when validation fails.

---

## Broker API unavailability at startup (AB1004 / balance sync failure) — §106 §116

If the Angel One RMS (Risk Management System) endpoint is unavailable when the container
starts, the `AccountRunner` balance sync will fail with errors like:

```
WARNING app.brokers.angel_client: event_type=BALANCE_FETCH_DEFERRED
  broker_account_id=A1 | Balance fetch failed; kind=FetchFailureKind.OTHER
  err=getRMS failed: {'message': 'Something Went Wrong, Please Try After Sometime',
  'errorcode': 'AB1004', ...}
```

### What this means

- The capital engine has **empty balance state** — it does not know available margin.
- `CAPITAL_FAIL_CLOSED_ON_MISSING_STATE=true` blocks all new entries until balance
  state is populated. The system starts but **cannot accept entry orders**.
- After 3 consecutive failures (configurable via `BALANCE_SYNC_ALERT_THRESHOLD`),
  the runner escalates to ERROR and emits a `balance_sync.persistent_failure` audit
  event. The first event is the signal to investigate.
- `/readyz` returns **503** with `"reason": "balance_sync_not_ready"` until at least
  one successful balance fetch completes.

### Recovery steps

1. **Check `/readyz`**: look for `"balance_sync_ready": false` and
   `"balance_sync_pending_runners"` to identify affected accounts.
2. **Check Angel One status**: AB1004 often means the RMS API is temporarily down or
   in a maintenance window (typical during 06:00–07:00 IST and 15:30–16:00 IST).
3. **Wait and retry**: the runner retries on its normal poll interval (default 15 s).
   Most AB1004 failures resolve within 2–5 minutes. Do not restart the container
   unless the failure persists beyond 10 minutes with no recovery.
4. **Look for recovery**: after a successful fetch, the log shows
   `balance_sync.first_success broker_account_id=A1` and `/readyz` returns 200 for
   the balance gate.
5. **If persistent (> 10 min)**: verify Angel One credentials in Postgres
   (`broker_credentials` table) and confirm the session token has not expired.
   Re-run `start-docker-secretstore.ps1` to refresh credentials if needed.

### When to abort

If balance sync does not recover within 30 minutes and the market is open:
- Activate the kill switch (global scope) to prevent any accidental entries.
- Bring the container down and investigate broker account status.

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
