# OCI VM Runtime Evidence

Last verified: 2026-06-06 14:02 UTC from the running OCI VM.
OI/ML shadow sidecar evidence was rechecked as present and dry-run only during
the same review.

The OCI VM is the production source of truth. This file intentionally records
what is running, including drift from repo templates. Secret values, private IPs,
OCIDs, broker identifiers, and tokens are redacted.

## Evidence Table

| Area | OCI VM evidence | Command/source | Verified current state | Notes |
|---|---|---|---|---|
| Host | `phoenix-vm`, `opc`, `/home/opc` | `hostname; date; whoami; pwd` | VM reachable through OCI Bastion; VM VNIC has no public IP | Do not document private IPs |
| Deployed repo path | `/opt/phoenix/app` | Compose labels and `git -C` | Active checkout lives under `/opt/phoenix/app` | `/opt/phoenix` also contains operator-owned runtime files |
| Active git commit/branch | `main`, `697409e...` plus live operator config drift | `git -C /opt/phoenix/app branch --show-current`, `rev-parse HEAD`, mounted config/env evidence | VM checkout is on `main` at `697409e...`; `/opt/phoenix/app/app/config/strategy_env.yaml` and `/opt/phoenix/phoenix-deploy.env` remain operator-owned runtime inputs | Deploy env image tag is `local-697409e`; backend and nginx have been restarted/reconciled against that tag |
| Compose project | `phoenix-oci-live` | `docker inspect ... Labels` | backend, nginx, watchdog, and Postgres have Compose labels | `phoenix-oci-postgres` is now managed by the opt-in `vm-local-postgres` profile |
| Compose files used | `/opt/phoenix/app/docker-compose.oci-live.yml`, `/opt/phoenix/phoenix-override.yml` | `com.docker.compose.project.config_files` labels | These are the active Phoenix Compose files for labelled containers | Runtime override must be treated as authoritative |
| Env file used | `/opt/phoenix/phoenix-deploy.env` | runtime scripts and Compose commands | Non-secret deploy env file exists on VM | Document names only, not values |
| Running Phoenix containers | `phoenix-oci-backend`, `phoenix-oci-web`, `phoenix-oci-watchdog`, `phoenix-oci-postgres` | `docker ps`, `docker inspect` | All four were running during audit | Aurelium containers also run on the host but are outside Phoenix docs |
| Stopped Phoenix containers | none shown by name | `docker ps -a` | `phoenix-oci-optimizer` is not present | Optimizer systemd units are also absent |
| Backend image | `phoenix-local-backend:local-697409e` | `docker inspect phoenix-oci-backend` | Local image, not OCIR | Source bind mounts are still active |
| Web image | `phoenix-local-nginx:local-697409e` | `docker inspect phoenix-oci-web` | Local image, not OCIR | Public nginx `/readyz` and `/health/summary` proxy to redacted backend endpoints; `/health/alerts` and `/health/mitigations` proxy JSON for the Alerts and Mitigations screens; Overview renders fallback values when that public summary omits internal schema, alert, watchdog, or account-count fields; `/manifest.json`, `/favicon.svg`, and `/favicon.ico` are served as static assets; stale `/static/*` assets return 404 instead of SPA HTML; frontend runtime failures render a visible recovery screen instead of a blank root |
| Database image | `postgres:16-alpine` | `docker inspect phoenix-oci-postgres` | Compose-managed VM-local Postgres container with Docker health status `healthy` | Container uses the existing `/opt/phoenix/pgdata` mount and password-file env, not a plaintext password value |
| Watchdog image | `docker:cli` | `docker inspect phoenix-oci-watchdog` | Docker CLI sidecar with no mounts | Recreated from the base no-socket compose service; no Docker socket or nginx stop/start capability is mounted |
| Backend command | `python -m app.main` via `docker-entrypoint.sh` | `docker inspect` | FastAPI backend runs in backend container | Port 8080 is container-only |
| Web command | `nginx -g 'daemon off;'` | `docker inspect` | nginx serves frontend and reverse proxy | Host ports 80 and 8443 |
| Restart policy | `unless-stopped` for Phoenix backend/web/postgres/watchdog | `docker inspect` | Containers restart unless stopped | `phoenix-oci-postgres` now reports Docker health status |
| Runtime mode | `HUB_AUTHORITATIVE` | `/health/summary` | Health summary reports hub-authoritative mode | `TRADE_MODE` env was not present in selected env check |
| Order path | `strategy_bridge_order_router` | `/health` | Broker-facing order path is the strategy bridge/order router path | Do not document legacy path as current authority |
| Backend env mode | `APP_ENV=production`, `CONTROL_PLANE_BACKEND=postgres`, `ENABLE_MULTI_HUB=true`, `DISABLE_CONTROL_TOWER_ROUTES=true` | selected `docker exec phoenix-oci-backend sh -lc 'printenv ...'` | Current backend env aligns with hub/Postgres authority | Secret-like values were redacted; Control Tower read-only status endpoints remain mounted, while mutating management controls stay disabled unless the LIVE mutation gate is explicitly enabled |
| Live strategy routing | EMA20-only: enabled strategy names are `ema20_strategy`; enabled NIFTY_IDX, BANKNIFTY_IDX, and NG_FUT allow only `ema20_strategy`; selector mappings contain no non-EMA names; `AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING=1` | container config validation and Postgres `strategy_configs` query | `exclusive_nifty_ce_buy`, `put_momentum_scalper`, and `nifty_weekly_credit_spreads` are disabled at config, instrument policy, selector, and control-plane layers | Latest readiness verification was green; routing changes do not clear or override kill-switch state |
| Database backend | `CONTROL_PLANE_PG_HOST=phoenix-oci-postgres`, `CONTROL_PLANE_PG_SSLMODE=prefer`, `LIVE_PG_SSL_SKIP_CHECK=true` | selected backend env | VM-local Postgres, not external OCI DB | Remote/cloud Postgres docs are non-current |
| Secret model | `/run/secrets/admin_api_key`, `/run/secrets/auth_token_secret`, `/run/secrets/control_plane_pg_password`, `/run/secrets/angel_postback_token`, `/run/secrets/admin_kill_switch_override` | mounts/env/runbook evidence and `scripts/validate-live-secret-perms.sh` | Secret values are mounted as files; kill-switch override is file-only; permission validator passes | Shared runtime secret files are `0440` for UID 100/root group compatibility, and backend-only secrets are `0400`; never copy values into docs or env examples |
| Backend mounts | `/opt/phoenix/logs`, `/opt/phoenix/state`, `/opt/phoenix/certs`, `/run/secrets/*`, plus source-file bind mounts | `docker inspect .Mounts` | Runtime includes host state/log/cert mounts and source overlays | Source bind mounts are current drift |
| Web mounts | `/opt/phoenix/nginx-ssl-prerendered.conf.template`, `/opt/phoenix/certs`, `/opt/phoenix/acme-challenge`, `/run/secrets/admin_api_key` | `docker inspect .Mounts` | nginx uses a prerendered host template | Repo nginx template is not directly mounted today; this host template was patched with `/health/alerts` and `/health/mitigations` before the latest nginx recreate |
| Postgres mounts | `/opt/phoenix/pgdata` to `/var/lib/postgresql/data` | `docker inspect .Mounts` | DB data is local VM disk path | Backup/restore docs must use this fact |
| Logs | `/opt/phoenix/logs`, date-partitioned app logs, audit JSONL, scheduler logs, cert renewal log | `find /opt/phoenix/logs` | Current logs include 2026-06-06 hardening/deployment evidence and root log files | `/opt/phoenix/logs` is writable by container UID |
| State files | `/opt/phoenix/state/risk_positions.json` and `.bak` | `ls -la /opt/phoenix/state` | Restart helper files exist | Not authoritative over Postgres |
| Health endpoints | backend container `/health` returns 200; backend `/readyz` returns 200 | `docker exec phoenix-oci-backend curl ...` | Backend liveness and readiness are green; LIVE universe health is part of the readiness gate | `/readyz` must fail when LIVE universe/quote-auth health is failed |
| nginx health | host `http://localhost/health`, `/readyz`, `/health/summary`, `/health/alerts`, and `/health/mitigations` returned through nginx | `curl` on VM and public curl probes | nginx proxies current health paths | Public nginx `/readyz` and `/health/summary` use redacted backend endpoints; Alerts/Mitigations endpoints return JSON |
| Release evidence endpoint | `/admin/release-evidence` returns 401 without admin key | `docker exec phoenix-oci-backend curl` | Endpoint exists and requires auth | Do not print admin key |
| Database tables | `audit_events`, `broker_accounts`, `broker_credentials`, `internal_position_records`, `kill_switch_state`, `order_submission_outbox`, `position_ownership_ledger`, `schema_migrations`, `strategy_configs`, `strategy_config_candidates`, `trades`, tenant/user entitlement tables, and others | `docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix` | Operational DB schema exists in VM-local Postgres | Backend container does not include `psql` |
| Cron/systemd | root cron starts at 03:30 UTC Mon-Fri and stops at 18:30 UTC Sun-Thu; root cert renewal; user safety watcher; weekly cleanup in `/etc/cron.d/phoenix-cleanup` | `crontab -l`, `sudo crontab -l`, `/etc/cron*` | Cron, not optimizer/reload systemd timers, controls current scheduled operations | `phoenix-runtime-secrets.service` exists but is inactive |
| Optimizer/reload timers | `phoenix-optimizer.*` and `phoenix-backend-reload.*` not found | `systemctl status` | Not installed on VM | Docs must not claim they are active |
| Watchdog behavior | watchdog inspect reports no mounts | `docker inspect phoenix-oci-watchdog --format '{{json .Mounts}}'` | Live watchdog is back on the base observe-only contract | Re-run `scripts/ops/recreate_oci_watchdog.sh` if future evidence shows Docker socket access or nginx stop/start actions |
| Storage headroom | root filesystem is 133G with 51G available and 63% used | `df -h /` after boot-volume expansion | Root disk headroom is back within the operational target | The boot volume was expanded to 150 GB and the filesystem was grown |
| OCI network | VM VNIC has private IP only, no public IP, no NSGs; subnet security list includes SSH, 80, 443, 8443, and ICMP | OCI network read-only inspection | VM is reached through private networking/Bastion/LB path | CIDRs and IPs redacted |

## 2026-06-06 Production Review / Hardening Backlog

The 2026-06-06 review created GitHub issues #343 through #354. Key production
blockers were:

- secret files and legacy Postgres env exposure required rotation and strict
  file permissions;
- `/readyz` could be green while universe/quote-auth refresh failed;
- root filesystem headroom was unsafe;
- public health/readiness responses exposed too much runtime detail;
- the live watchdog and VM-local Postgres containers drifted from the repo
  compose contract;
- the Phoenix VM also hosted unrelated public workloads.

Remediation scripts added for this review:

- `scripts/validate-live-secret-perms.sh`
- `scripts/ops/harden_oci_file_permissions.sh`
- `scripts/ops/oci_storage_report.sh`
- `scripts/ops/recreate_oci_watchdog.sh`
- `scripts/ops/adopt_oci_postgres_compose.sh`

Implemented and deployed in the hardening pass:

- secret-file permissions hardened and validated on the VM;
- `/readyz` now fails closed on LIVE universe/quote-auth health failure;
- backend start/redeploy gates wait for `/readyz`;
- public nginx `/readyz` and `/health/summary` use redacted backend endpoints;
- the frontend Overview page tolerates the redacted public health summary and
  shows unavailable diagnostics as fallback values instead of failing render;
- public nginx `/health/alerts` and `/health/mitigations` now proxy JSON, and
  the Alerts, Mitigations, and Safety screens tolerate omitted public fields;
- root disk was expanded and Docker build cache was pruned;
- `phoenix-oci-postgres` was adopted into the Compose `vm-local-postgres`
  profile with Docker health metadata;
- `phoenix-oci-watchdog` was recreated with no Docker socket and no mounts.

Remaining backlog items require credential or infrastructure owner action:

- rotate the previously exposed secret values and broker credentials;
- isolate or explicitly risk-accept the unrelated public workloads that still
  share the Phoenix VM.

## 2026-05-20 Position-Authority Recovery

Commit `e1f9ddb` was deployed as `phoenix-local-backend:local-e1f9ddb`; nginx
remains `phoenix-local-nginx:local-349d55f`. During deployment, the recovery
endpoint reported two `RECOVERY_PENDING` records for the same NIFTY contract and
broker account with `broker_evidence.status=flat`. Both records were cleared via
`POST /admin/state/clear-position-record` with `force=false` and audit events
were emitted. Final evidence after backend restart:

- `/readyz` returned `ready=true`.
- `/dashboard/status` returned `status=ok`, `readiness.ready=true`.
- `/admin/state/position-authority/recovery` returned `recovery_record_count=0`.
- `kill_switch_active_count=0`.
- `terminal_position_records_nonzero_net_qty_count=0`.

## 2026-06-03 Control Tower / Kill-Switch Recovery Deploy

Commit `4288919` was deployed as `phoenix-local-backend:local-4288919` and
`phoenix-local-nginx:local-4288919`. Post-deploy evidence:

- backend and nginx containers are Docker-healthy.
- backend `/health` and host `/health` returned 200.
- backend `/readyz` and host `/readyz` returned 503 because one durable
  kill switch is active; divergence is false and legacy kill switch is inactive.
- `GET /api/control_tower/status` with admin auth returned 200 and
  `capability.read_only=true`, `mutation_enabled=false`,
  `routes_disabled=true`.
- Control Tower read visibility is therefore available while LIVE management
  mutations stay disabled by deployment gates and the active durable kill switch.

## 2026-06-03 Put-Momentum Stale Exit Retry Fix Deploy

Commit `132e0ea` was deployed as `phoenix-local-backend:local-132e0ea` and
`phoenix-local-nginx:local-132e0ea` after live monitoring observed repeated
`ORDER_EXIT_REJECTED_NO_POSITION_EVIDENCE` events for
`put_momentum_scalper` after broker-flat/no-position evidence.

Post-deploy evidence:

- backend and nginx containers are Docker-healthy on the pinned `local-132e0ea`
  images.
- backend `/health` and host `/health` returned 200.
- backend `/readyz` and host `/readyz` returned 503 only because one durable
  global kill switch remains active from the 2026-06-03 11:22 IST
  floating-drawdown trip; this deploy did not clear the kill switch.
- deployed import probe returned `put_momentum_import_ok`.
- no new `ORDER_EXIT_REJECTED_NO_POSITION_EVIDENCE` entries appeared after the
  12:29 IST deployment/restart through the 12:35 IST five-minute boundary.

## 2026-06-03 EMA20-Only Live Routing Update

Live operator change applied after repo-local config edits:

- `/opt/phoenix/app/app/config/strategy_env.yaml` was backed up to
  `/opt/phoenix/app/app/config/strategy_env.yaml.bak_20260603_ema20_only` and
  updated so only `ema20_strategy` is enabled in LIVE routing.
- `/opt/phoenix/phoenix-deploy.env` was backed up to
  `/opt/phoenix/phoenix-deploy.env.bak_20260603_ema20_only` and updated with
  `AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING=1`.
- Postgres `strategy_configs` rows were verified as `ema20_strategy=true` and
  `exclusive_nifty_ce_buy=false`, `put_momentum_scalper=false`,
  `nifty_weekly_credit_spreads=false`.
- The backend was recreated with the documented compose files and reported
  Docker healthy after restart.
- Container validation reported `enabled_strategy_names=['ema20_strategy']`,
  enabled NIFTY_IDX/BANKNIFTY_IDX/NG_FUT allow-lists containing only
  `ema20_strategy`, selector max active value `1`, and no non-EMA selector names
  for enabled underlyings.
- Image tag remains `phoenix-local-backend:local-132e0ea`; this was a
  config/env/control-plane update, not a new image build.
- The durable kill switch remained active during verification; this change did
  not clear it.

## 2026-06-04 Live Monitor Remediations

Live monitoring for the 09:00-15:30 IST window started at 09:26 IST, so
09:00-09:26 was covered retrospectively and later scans ran from the VM-side
monitor under `/opt/phoenix/logs/live_monitor_20260604`.

Findings and remediation:

- Issue #339: startup daily-level fetches for NIFTY_IDX and BANKNIFTY_IDX timed
  out because `DailyLevelsCache` used a direct Angel HTTPS connection instead
  of the existing proxy-aware broker connection helper. The code path now uses
  the proxy-aware helper and has unit coverage.
- Issue #340: startup loaded three stale
  `__pending__:system::position_trailing_lock` ownership rows from 2026-05-27
  and 2026-05-29. Root cause was terminal system-exit fills applying ownership
  to the original strategy while leaving the submitting system strategy's
  pending lock unreleased. Terminal-fill handling now releases the original
  submitting strategy's pending lock when the fill owner differs.
- Issue #341: the subscription reconcile watchdog emitted a warning every
  minute for expired SHADOW subscription `A2@A2`
  (`2026-05-24 21:07 IST` through `2026-05-27 21:07 IST`). This did not affect
  the live `A1` runner, readiness, degraded scopes, or kill-switch state. The
  reconcile path now keeps warning-level logs for live-affecting disabled
  states, while expired PAPER/SHADOW subscriptions log at info on first
  observation and debug on unchanged repeats.
- Issue #342: after the `83c32da` deploy, startup still emitted
  `startup.ssl_warning` for `LIVE_PG_SSL_SKIP_CHECK=true` with
  `CONTROL_PLANE_PG_SSLMODE=prefer`. Evidence showed the DB host was
  `phoenix-oci-postgres` and `docker port phoenix-oci-postgres` returned no
  published ports, so this is the documented VM-local Docker Postgres topology,
  not an externally reachable database. Startup SSL validation now logs this
  audited local-host exception at info level while preserving warning/error
  behavior for unknown or remote/cloud Postgres deployments.
- One-time cleanup deleted exactly three stale pending rows after evidence
  showed `/readyz ready=true`, position-authority recovery count `0`, active
  outbox count `0`, and no matching active internal positions. Post-cleanup
  query returned zero `__pending__:system::position_trailing_lock` rows.
- The 09:07 IST kill-switch clear was an authenticated admin action and left
  `/readyz` green with `kill_switch_active_count=0`.
- A transient 14:00 IST universe quote fetch timeout for `NSE:1` recovered on
  retry; the universe build completed at 14:00:18 IST and no further quote
  fetch failures were present through the 15:30 IST cutoff.
- Final monitor poll at 15:31:57 IST reported `status=completed`; the last
  health samples through 15:29:13 IST were `health_code=200`,
  `readyz_code=200`, `ready=True`, `kill_switch_active_count=0`,
  `divergence=False`, `degraded_scope_count=0`, and
  `failed_runner_count=0`.

## 2026-05-22 BANKNIFTY Position-Authority Recovery

Live evidence on 2026-05-22 showed `A1 BANKNIFTY 2026-05-26 54100 CE`
broker position was flat while Phoenix still had two internal records for the
same contract:

- `ema20_strategy` entry record: `OPEN`, side `SELL`, `net_qty=-30`.
- `system::position_trailing_lock` exit record: `DEGRADED`, side `BUY`,
  `net_qty=30`, reason
  `illegal_transition_blocked:RECONCILING_to_PARTIALLY_EXITED:partial_exit_fill_observed`.

Broker-flat recovery evidence allowed both records to be cleared with
`POST /admin/state/clear-position-record` using `force=false`. Audit events
recorded `broker_net_qty_at_clear=0.0`. The backend was restarted only because
the deployed endpoint did not yet recover the in-memory degraded-scope marker;
after restart, `/health/summary` returned `status=ok`, `readiness.ready=true`,
`degraded_scope_count=0`, no degraded reasons, and zero firing alerts.

The displayed kill-switch reason
`risk_manager_auto: floating_drawdown source=tick:NG_OTM_CE_340` was from a
separate historical global kill-switch trip at `2026-05-21T14:35:54Z`, cleared
and rearmed at `2026-05-21T18:47:42Z`. It was not the cause of the BANKNIFTY
position-authority degradation on 2026-05-22.

## 2026-05-25 Manual-Order Degradation Mitigation

Live evidence on 2026-05-25 showed a manual Angel One order sequence on
`NIFTY 2026-05-26 24000 CE`: a direct broker SELL of 65 at 12:32 IST followed
by a direct broker BUY of 65 at 14:27 IST. Phoenix ingested both fills under
the sentinel strategy `__external__`, but a stale zero-quantity
`system::position_trailing_lock` internal position record remained
`RECONCILING`. The outbox contained repeated terminal non-fill/rejection
attempts for the same trailing-lock scope, and readiness degraded with
`position_authority_degraded` even though broker evidence was flat.

Commit `8564b9b` added the mitigation and was later included in the
`e7f1e29` live deployment:

- Position trailing lock consults broker/current-position evidence first.
- If broker evidence is flat, trailing lock does not continue managing stale
  external/manual-owned internal records.
- If broker evidence shows a live position, trailing lock can manage the
  externally/manual-owned broker position.
- After one terminal non-fill or broker rejection for a specific broker
  position signature, repeat trailing-lock attempts for that same scope are
  suppressed until the broker position changes or disappears.
- Broker-flat auto-recovery runs after successful order sync and external-fill
  reconciliation. It can clear stale zero-quantity `RECONCILING`, `DEGRADED`,
  or `RECOVERY_PENDING` internal position records only when position and order
  snapshots are fresh, broker position is flat for the contract, and no active
  matching broker order exists.

Final deployment evidence for `e7f1e29`:

- backend image `phoenix-local-backend:local-e7f1e29`
- nginx image `phoenix-local-nginx:local-e7f1e29`
- backend `/readyz` returned HTTP 200 with `ready=true`,
  `degraded_scope_count=0`, `position_state_counts={}`, and `firing_count=0`
- host `/health` and `/readyz` returned HTTP 200 through nginx

## OI/ML Shadow Sidecar Evidence

Verified on 2026-05-23 IST:

| Area | Verified current state |
|---|---|
| Purpose | Dry-run OI/ML CE seller validation; no live order routing |
| Checkout | `/opt/phoenix/oi-ml-shadow-src` |
| Compose file | `/opt/phoenix/oi-ml-shadow.yml` |
| Image | `phoenix-oi-ml-shadow:oi-ml-shadow-bd999cd` |
| Container | `phoenix-oi-ml-shadow`, no host ports published |
| Scorer | Smoke deployment uses `OI_ML_SHADOW_SCORER=constant` |
| Risk posture | `OI_ML_SHADOW_ALLOW_NAKED=false`; sidecar records shadow intents only |
| Health visibility | Backend observes the external sidecar with `OI_ML_SHADOW_HEALTH_ENABLED=true`; sidecar Docker healthcheck runs `python -m app.strategies.oi_ml.shadow_health` |
| Tables | `public.option_chain_1m`, `public.oi_ml_shadow_order_intents`, `public.option_chain_validation_reports` |
| Expiry handling | Startup resolves listed NIFTY expiry from Angel scrip master; latest observed `calendar_default=2026-05-28 listed=2026-05-26` |
| Input hardening | Provider now fetches/stamps NIFTY spot and India VIX context LTPs for option-chain rows |
| Broker proxy/session | Sidecar forwards backend broker proxy env and reuses the Angel quote session during snapshotting |
| Smoke proof | 2026-05-18 21:11 IST off-market run fetched/stored `220` NIFTY rows through Angel FULL/LTP quote APIs; no shadow intent was recorded |
| NSE validation | Falls back to NSE `liveEquity-derivatives` rows when the classic option-chain JSON endpoint is empty; latest smoke returned `288` reference rows and `288` compared contracts |
| IV handling | Missing Angel IV is enriched at read time only from fresh exact-contract `nse_web` rows that contain IV; the live-derivatives fallback does not supply IV/bid/ask |
| Remaining gate | Market-session hard-field completeness and fresh source timestamps still need proof before promotion beyond shadow |

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
option_chain_validation_reports
```

## Current Risk Register

| Severity | Risk | Evidence | Required action |
|---|---|---|---|
| P1 | Production docs previously described OCIR images and external OCI DB as current | VM runs `phoenix-local-*` images and `phoenix-oci-postgres` | Keep docs tied to VM evidence until deployment changes |
| P1 | Source-file bind mounts in backend | backend mounts multiple `/opt/phoenix/app/app/...` files | Treat as current drift; remove only through an approved deployment change |
| P1 | Secret values exposed before hardening still require rotation | 2026-06-06 production review | Rotate admin/auth/DB/broker/dashboard credentials through approved owner workflows |
| P1 | Phoenix VM still hosts unrelated public workloads | OCI VM process/container inventory | Move the co-tenant workloads or formally risk-accept the shared host |
| P2 | Optimizer/reload timers are documented in older docs but not installed | `systemctl status` | Mark optimizer/reload timer docs as non-current until installed |
| P2 | `/api/health` is not a health endpoint through nginx | curl returned SPA HTML | Use `/health` and `/readyz` |
