# OCI VM Runtime Evidence

Last verified: 2026-05-17 12:25 UTC from the running OCI VM.
OI/ML shadow sidecar evidence was added on 2026-05-18 IST.

The OCI VM is the production source of truth. This file intentionally records
what is running, including drift from repo templates. Secret values, private IPs,
OCIDs, broker identifiers, and tokens are redacted.

## Evidence Table

| Area | OCI VM evidence | Command/source | Verified current state | Notes |
|---|---|---|---|---|
| Host | `phoenix-vm`, `opc`, `/home/opc` | `hostname; date; whoami; pwd` | VM reachable through OCI Bastion; VM VNIC has no public IP | Do not document private IPs |
| Deployed repo path | `/opt/phoenix/app` | Compose labels and `git -C` | Active checkout lives under `/opt/phoenix/app` | `/opt/phoenix` also contains operator-owned runtime files |
| Active git commit/branch | `main`, `1a2cc47d8cb23fbc9b60e5eea8e5841e10d79ccd` | `git -C /opt/phoenix/app branch --show-current`, `rev-parse HEAD` | VM checkout is on `main` at `1a2cc47` | `git status --short` shows untracked `docker-compose.oci-postgres.yml` |
| Compose project | `phoenix-oci-live` | `docker inspect ... Labels` | backend, nginx, and watchdog have Compose labels | `phoenix-oci-postgres` has no Compose labels |
| Compose files used | `/opt/phoenix/app/docker-compose.oci-live.yml`, `/opt/phoenix/phoenix-override.yml` | `com.docker.compose.project.config_files` labels | These are the active Phoenix Compose files for labelled containers | Runtime override must be treated as authoritative |
| Env file used | `/opt/phoenix/phoenix-deploy.env` | runtime scripts and Compose commands | Non-secret deploy env file exists on VM | Document names only, not values |
| Running Phoenix containers | `phoenix-oci-backend`, `phoenix-oci-web`, `phoenix-oci-watchdog`, `phoenix-oci-postgres` | `docker ps`, `docker inspect` | All four were running during audit | Aurelium containers also run on the host but are outside Phoenix docs |
| Stopped Phoenix containers | none shown by name | `docker ps -a` | `phoenix-oci-optimizer` is not present | Optimizer systemd units are also absent |
| Backend image | `phoenix-local-backend:local-1a2cc47`, image ID `sha256:35feef34...` | `docker inspect phoenix-oci-backend` | Local image, not OCIR | Created 2026-05-13 |
| Web image | `phoenix-local-nginx:local-1a2cc47`, image ID `sha256:c7e2220...` | `docker inspect phoenix-oci-web` | Local image, not OCIR | Created 2026-05-15 |
| Database image | `postgres:16-alpine`, image ID `sha256:4e6e670...` | `docker inspect phoenix-oci-postgres` | VM-local Postgres container | No Docker healthcheck |
| Watchdog image | `docker:cli`, image ID `sha256:17b5c235...` | `docker inspect phoenix-oci-watchdog` | Docker CLI sidecar | Has Docker socket mount |
| Backend command | `python -m app.main` via `docker-entrypoint.sh` | `docker inspect` | FastAPI backend runs in backend container | Port 8080 is container-only |
| Web command | `nginx -g 'daemon off;'` | `docker inspect` | nginx serves frontend and reverse proxy | Host ports 80 and 8443 |
| Restart policy | `unless-stopped` for Phoenix backend/web/postgres/watchdog | `docker inspect` | Containers restart unless stopped | `phoenix-oci-postgres` has no health status |
| Runtime mode | `HUB_AUTHORITATIVE` | `/health/summary` | Health summary reports hub-authoritative mode | `TRADE_MODE` env was not present in selected env check |
| Order path | `strategy_bridge_order_router` | `/health` | Broker-facing order path is the strategy bridge/order router path | Do not document legacy path as current authority |
| Backend env mode | `APP_ENV=production`, `CONTROL_PLANE_BACKEND=postgres`, `ENABLE_MULTI_HUB=true`, `DISABLE_CONTROL_TOWER_ROUTES=true` | selected `docker exec phoenix-oci-backend sh -lc 'printenv ...'` | Current backend env aligns with hub/Postgres authority | Secret-like values were redacted |
| Database backend | `CONTROL_PLANE_PG_HOST=phoenix-oci-postgres`, `CONTROL_PLANE_PG_SSLMODE=prefer`, `LIVE_PG_SSL_SKIP_CHECK=true` | selected backend env | VM-local Postgres, not external OCI DB | Remote/cloud Postgres docs are non-current |
| Secret model | `/run/secrets/admin_api_key`, `/run/secrets/auth_token_secret`, `/run/secrets/control_plane_pg_password`, `/run/secrets/angel_postback_token` | mounts/env/runbook evidence | Secret values are mounted as files | Never copy values into docs or env examples |
| Backend mounts | `/opt/phoenix/logs`, `/opt/phoenix/state`, `/opt/phoenix/certs`, `/run/secrets/*`, plus source-file bind mounts | `docker inspect .Mounts` | Runtime includes host state/log/cert mounts and source overlays | Source bind mounts are current drift |
| Web mounts | `/opt/phoenix/nginx-ssl-prerendered.conf.template`, `/opt/phoenix/certs`, `/opt/phoenix/acme-challenge`, `/run/secrets/admin_api_key` | `docker inspect .Mounts` | nginx uses a prerendered host template | Repo nginx template is not directly mounted today |
| Postgres mounts | `/opt/phoenix/pgdata` to `/var/lib/postgresql/data` | `docker inspect .Mounts` | DB data is local VM disk path | Backup/restore docs must use this fact |
| Logs | `/opt/phoenix/logs`, date-partitioned app logs, audit JSONL, scheduler logs, cert renewal log | `find /opt/phoenix/logs` | Current logs under `/opt/phoenix/logs/2026-05-17` and root log files | `/opt/phoenix/logs` is writable by container UID |
| State files | `/opt/phoenix/state/risk_positions.json` and `.bak` | `ls -la /opt/phoenix/state` | Restart helper files exist | Not authoritative over Postgres |
| Health endpoints | backend container `/health`, `/ready`, `/readyz`, `/health/summary` return 200 | `docker exec phoenix-oci-backend curl ...` | Backend ready at container-local port 8080 | Host `localhost:8080` is not exposed |
| nginx health | host `http://localhost/health`, `http://localhost/readyz`, `https://localhost:8443/health`, `https://localhost:8443/readyz` return 200 | `curl -k` on VM | nginx proxies current health paths | `/api/health` falls through to SPA and is not a health API |
| Release evidence endpoint | `/admin/release-evidence` returns 401 without admin key | `docker exec phoenix-oci-backend curl` | Endpoint exists and requires auth | Do not print admin key |
| Database tables | `audit_events`, `broker_accounts`, `broker_credentials`, `internal_position_records`, `kill_switch_state`, `order_submission_outbox`, `position_ownership_ledger`, `schema_migrations`, `strategy_configs`, `trades`, tenant/user entitlement tables, and others | `docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix` | Operational DB schema exists in VM-local Postgres | Backend container does not include `psql` |
| Cron/systemd | root cron starts at 03:30 UTC Mon-Fri and stops at 18:30 UTC Sun-Thu; root cert renewal; user safety watcher; weekly cleanup in `/etc/cron.d/phoenix-cleanup` | `crontab -l`, `sudo crontab -l`, `/etc/cron*` | Cron, not optimizer/reload systemd timers, controls current scheduled operations | `phoenix-runtime-secrets.service` exists but is inactive |
| Optimizer/reload timers | `phoenix-optimizer.*` and `phoenix-backend-reload.*` not found | `systemctl status` | Not installed on VM | Docs must not claim they are active |
| Watchdog behavior | logs show repeated backend fail counts and nginx stop/start recovery | `docker logs phoenix-oci-watchdog` | Watchdog actively changes nginx state | Older "observe-only" docs are wrong |
| Log abnormalities | backend/web abnormal grep did not show recent critical errors; watchdog shows repeated fail/recover events | `docker logs --tail=500 ... grep` | No recent backend criticals found in sampled tail | Watchdog churn remains an operational warning |
| OCI network | VM VNIC has private IP only, no public IP, no NSGs; subnet security list includes SSH, 80, 443, 8443, and ICMP | OCI network read-only inspection | VM is reached through private networking/Bastion/LB path | CIDRs and IPs redacted |

## OI/ML Shadow Sidecar Evidence

Verified on 2026-05-18 IST:

| Area | Verified current state |
|---|---|
| Purpose | Dry-run OI/ML CE seller validation; no live order routing |
| Checkout | `/opt/phoenix/oi-ml-shadow-src` |
| Compose file | `/opt/phoenix/oi-ml-shadow.yml` |
| Image | `phoenix-oi-ml-shadow:oi-ml-shadow-9e91b77` |
| Container | `phoenix-oi-ml-shadow`, no host ports published |
| Scorer | Smoke deployment uses `OI_ML_SHADOW_SCORER=constant` |
| Risk posture | `OI_ML_SHADOW_ALLOW_NAKED=false`; sidecar records shadow intents only |
| Tables | `public.option_chain_1m`, `public.oi_ml_shadow_order_intents` |
| Expiry handling | Startup resolves listed NIFTY expiry from Angel scrip master; observed `calendar_default=2026-05-21 listed=2026-05-19` |
| Input hardening | Provider now fetches/stamps NIFTY spot and India VIX context LTPs for option-chain rows |
| Broker proxy/session | Sidecar forwards backend broker proxy env and reuses the Angel quote session during snapshotting |
| Smoke proof | 2026-05-18 21:11 IST off-market run fetched/stored `220` NIFTY rows through Angel FULL/LTP quote APIs; no shadow intent was recorded |
| Remaining gate | Market-session FULL quote field completeness is not yet proven; off-market smoke still had missing `iv` and stale source timestamps |

Operator runbook: [OI/ML Shadow Sidecar Runbook](runbooks/oi_ml_shadow_sidecar.md).

## Current Phoenix Tables Observed

The VM-local Postgres database reported these public tables:

```text
audit_events
bar_regime
broker_accounts
broker_credentials
canonical_strategy_registry
circuit_breaker_state
eod_states
indicator_bars
internal_position_records
kill_switch_state
order_submission_outbox
pnl_snapshots
position_ownership_ledger
position_trailing_lock_inflight
position_trailing_lock_state
profit_lock_state
refresh_tokens
revoked_tokens
schema_migrations
slippage_records
step_up_tokens
strategy_configs
strategy_migration_log
subscriptions
sweep_states
tenants
trade_decision_lineage
trade_processed_markers
trades
user_broker_account_entitlements
user_tenant_entitlements
users
option_chain_1m
oi_ml_shadow_order_intents
```

## Current Risk Register

| Severity | Risk | Evidence | Required action |
|---|---|---|---|
| P1 | Production docs previously described OCIR images and external OCI DB as current | VM runs `phoenix-local-*` images and `phoenix-oci-postgres` | Keep docs tied to VM evidence until deployment changes |
| P1 | Source-file bind mounts in backend | backend mounts multiple `/opt/phoenix/app/app/...` files | Treat as current drift; remove only through an approved deployment change |
| P1 | VM-local Postgres has no Docker healthcheck and is not Compose-labelled | `phoenix-oci-postgres` inspect | Add operational DB health evidence before relying on unattended restart |
| P1 | Watchdog can stop nginx | watchdog logs and command | Runbooks must mention this behavior and its recovery implications |
| P2 | Optimizer/reload timers are documented in older docs but not installed | `systemctl status` | Mark optimizer/reload timer docs as non-current until installed |
| P2 | `/api/health` is not a health endpoint through nginx | curl returned SPA HTML | Use `/health` and `/readyz` |
