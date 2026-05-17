# Documentation Audit

Audit date: 2026-05-17.

Scope: repository documentation, env examples, Compose comments, and operator
script headers were checked against the running OCI VM. The OCI VM overrides all
repo docs and historical plans.

## Runtime Evidence Summary

| Area | Verified current state |
|---|---|
| Repo path | `/opt/phoenix/app` |
| Active git | `main` at `1a2cc47d8cb23fbc9b60e5eea8e5841e10d79ccd`; untracked `docker-compose.oci-postgres.yml` |
| Compose project | `phoenix-oci-live` |
| Compose files | `/opt/phoenix/app/docker-compose.oci-live.yml`, `/opt/phoenix/phoenix-override.yml` |
| Env file | `/opt/phoenix/phoenix-deploy.env` |
| Backend | `phoenix-oci-backend`, `phoenix-local-backend:local-1a2cc47`, healthy |
| Web | `phoenix-oci-web`, `phoenix-local-nginx:local-1a2cc47`, healthy |
| Database | VM-local `phoenix-oci-postgres`, `postgres:16-alpine`, no healthcheck |
| Watchdog | `phoenix-oci-watchdog`, actively stops/starts nginx on backend health failures |
| Runtime mode | `/health/summary` reports `HUB_AUTHORITATIVE`; `/health` reports `strategy_bridge_order_router` |
| Health endpoints | backend container `/health`, `/ready`, `/readyz`, `/health/summary`; nginx host `/health`, `/readyz` |
| Logs | `/opt/phoenix/logs`; date-partitioned app logs plus scheduler/cert/safety logs |
| Secret model | `/run/secrets/*`; docs may list names only |

Full evidence: [OCI VM Runtime Evidence](OCI_VM_RUNTIME.md).

## Documentation Inventory

| Path | Type | Claimed purpose | OCI VM match status | Action |
|---|---|---|---|---|
| `README.md` | entrypoint | repo/operator index | PARTIALLY_STALE | UPDATE |
| `ABOUTME.md` | summary | plain-language overview | PARTIALLY_STALE | UPDATE |
| `ARCHITECTURE.md` | architecture | production contract | PARTIALLY_STALE | UPDATE |
| `Agents.md` | agent instruction | review rules | MATCHES_OCI_VM | KEEP |
| `docs/OCI_VM_RUNTIME.md` | evidence | current VM state | MATCHES_OCI_VM | KEEP |
| `docs/DOCUMENTATION_AUDIT.md` | audit | doc inventory/mismatch | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/oci_live_deployment.md` | runbook | OCI operations | CONTRADICTS_OCI_VM | UPDATE |
| `docs/runbooks/oi_ml_shadow_sidecar.md` | runbook | OI/ML shadow sidecar progress and gates | MATCHES_OCI_VM | KEEP |
| `docs/runbooks/oci-live.env.example` | env template | OCI env names | PARTIALLY_STALE | UPDATE |
| `phoenix-override.yml.example` | override template | OCI override | CONTRADICTS_OCI_VM | UPDATE |
| `docker-compose.oci-live.yml` | Compose/comments | OCI base manifest | PARTIALLY_STALE | UPDATE |
| `docker-compose.live.single.yml` | Compose/comments | Docker Desktop live | OBSOLETE | UPDATE |
| `.env.example` | env template | local/staging env | PARTIALLY_STALE | UPDATE |
| `docker.env` | env template | local dev | ROADMAP_ONLY | KEEP |
| `cloudrun.env` | env template | Cloud Run reference | ROADMAP_ONLY | UPDATE |
| `docs/runbooks/docker_desktop_live_deployment.md` | runbook | Docker Desktop LIVE | OBSOLETE | UPDATE |
| `docs/runbooks/cloud_run_live_deployment.md` | runbook | Cloud Run reference | ROADMAP_ONLY | UPDATE |
| `docs/runbooks/cloudrun-live.env.example` | env template | Cloud Run env | ROADMAP_ONLY | UPDATE |
| `docs/runbooks/update_broker_credentials.md` | runbook | broker credential rotation | UNSAFE_FOR_LIVE | UPDATE |
| `docs/runbooks/strategy_runtime_diagnostics.md` | runbook | strategy diagnostics | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/capital_limits_configuration.md` | runbook | capital limits | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/restore_drill.md` | runbook | DB restore drill | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/blue_green_cutover.md` | runbook | blue/green cutover | ROADMAP_ONLY | UPDATE |
| `docs/runbooks/break_glass_flatten.md` | runbook | emergency flatten | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/dashboard-kill-switch.md` | runbook | dashboard kill switch | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/kill_switch.md` | runbook | kill switch | UNKNOWN_NEEDS_EVIDENCE | KEEP |
| `docs/runbooks/release_evidence.md` | runbook | release evidence | PARTIALLY_STALE | KEEP |
| `docs/runbooks/oci_runtime_hardening.md` | runbook | runtime drift hardening | MATCHES_OCI_VM | KEEP |
| `docs/runbooks/resolve_orphan_review.md` | runbook | orphan review | UNKNOWN_NEEDS_EVIDENCE | KEEP |
| `docs/runbooks/ema20_tp_pct_tuning.md` | runbook | tuning diagnostics | ROADMAP_ONLY | KEEP |
| `docs/STRATEGIES.md` | reference | strategy catalog | UNKNOWN_NEEDS_EVIDENCE | KEEP |
| `docs/Flowchart.md` | architecture reference | flow diagrams | PARTIALLY_STALE | KEEP |
| `docs/kpis_slos.md` | observability | KPI/SLO reference | PARTIALLY_STALE | KEEP |
| `docs/parameters.md` | reference | strategy parameters | UNKNOWN_NEEDS_EVIDENCE | KEEP |
| `docs/nse-holidays.txt` | scheduler input | NSE holiday list | UNKNOWN_NEEDS_EVIDENCE | KEEP |
| `docs/release-evidence/README.md` | evidence docs | evidence folder guide | PARTIALLY_STALE | KEEP |
| `docs/release-evidence/restore_drill_TEMPLATE.md` | template | restore evidence | PARTIALLY_STALE | KEEP |
| `docs/release-evidence/restore_drill_20260425.md` | historical evidence | restore drill record | OBSOLETE | KEEP |
| `docs/release-evidence/*.json` | historical evidence | prior release evidence | OBSOLETE | KEEP |
| `docs/archive/ARCHIVE.md` | archive index | archived docs | MATCHES_OCI_VM | KEEP |
| `docs/archive/phoenix_backlog.csv` | archive | historical backlog | OBSOLETE | KEEP |
| `scripts/replay/REPLAY.md` | developer doc | replay harness | ROADMAP_ONLY | KEEP |
| `scripts/start-phoenix.sh` | script header | scheduled start | PARTIALLY_STALE | KEEP |
| `scripts/stop-phoenix.sh` | script header | scheduled stop | PARTIALLY_STALE | KEEP |
| `scripts/fetch-secrets.sh` | script header | secret fetch | PARTIALLY_STALE | KEEP |
| `scripts/ops/*.sh`, `scripts/ops/*.ps1` | script headers | ops helpers | UNKNOWN_NEEDS_EVIDENCE | KEEP |
| `app/config/strategy_env.yaml` | config comments | strategy/runtime config | PARTIALLY_STALE | KEEP |
| `app/config/universe.yaml` | config comments | universe config | UNKNOWN_NEEDS_EVIDENCE | KEEP |

## Mismatch Review

| Severity | Document | Claim | OCI VM evidence | Risk | Required documentation change |
|---|---|---|---|---|---|
| P1 | `README.md`, `ARCHITECTURE.md`, `docs/runbooks/oci_live_deployment.md`, `docker-compose.oci-live.yml` | OCI path uses OCIR images and external OCI Postgres | VM runs `phoenix-local-*` images and `phoenix-oci-postgres` | Wrong restart/deploy/DB assumptions | Mark base manifest as secondary; document current local image/local DB runtime |
| P1 | `docs/runbooks/oci_live_deployment.md` | watchdog is observe-only | `phoenix-oci-watchdog` command/logs stop and start nginx | Operator may misread nginx outages | Document active nginx stop/start behavior |
| P1 | `phoenix-override.yml.example` | no source-code bind mounts, repo nginx template mount | VM override has source bind mounts and prerendered nginx template | Recreated runtime would differ from production | Make example mirror current VM and label drift |
| P1 | `docs/runbooks/oci-live.env.example` | external DB endpoint and OCIR tag are current | VM uses `CONTROL_PLANE_PG_HOST=phoenix-oci-postgres` and local images | Failed deploy or wrong DB target | Update template to VM-local DB and local image tag shape |
| P0 | `docs/runbooks/update_broker_credentials.md` | select `api_key`, `client_code`, `client_public_ip` during verification | Broker credential table exists on VM | Secret leakage into terminals/tickets | Replace value selection with boolean presence checks |
| P1 | `docs/runbooks/docker_desktop_live_deployment.md` | Docker Desktop is current LIVE guidance | VM is OCI-only production | Operator could follow wrong restart/env path | Mark non-current production |
| P1 | `docs/runbooks/blue_green_cutover.md` | blue/green cutover usable | VM has one Compose project and one local DB | False operational confidence | Mark roadmap-only |
| P1 | `docs/runbooks/restore_drill.md` | generic/external DB examples apply | VM DB is local Postgres container | Wrong restore target | Add current OCI VM DB note |
| P2 | Cloud Run docs/env | future Cloud Run target | no Cloud Run evidence on VM | Confusion | Mark non-current roadmap |
| P2 | Optimizer/reload runbook sections | systemd timers installed | `systemctl status` shows units not found | Operators chase absent timers | Mark absent in runtime evidence and OCI runbook |
| P2 | Health docs | `/api/health` as health path | nginx returns SPA for `/api/health` | False health check | Document `/health` and `/readyz` only |

## Final Documentation Map

| Final document | Purpose | Must contain | Must not contain | Source documents |
|---|---|---|---|---|
| `README.md` | concise operator entrypoint | current VM state, reading order, safe commands | Docker Desktop/Cloud Run as current | VM evidence, old README |
| `docs/OCI_VM_RUNTIME.md` | evidence snapshot | containers, images, mounts, endpoints, DB, cron, risks | secrets or private IPs | OCI commands/logs |
| `docs/runbooks/oci_live_deployment.md` | executable current OCI runbook | exact VM commands, paths, health, DB, logs, recovery | aspirational OCIR/external DB claims | VM evidence, old OCI runbook |
| `ARCHITECTURE.md` | production contract with current runtime preface | current VM evidence and safety invariants | stale "current recommended" deployment claims | old architecture, VM evidence |
| `ABOUTME.md` | plain-language non-authoritative summary | current VM overview | authoritative claims | old ABOUTME, VM evidence |
| `phoenix-override.yml.example` | current override template | current local images/local DB/bind mounts | target-only invariants as current | VM override |
| `docs/runbooks/oci-live.env.example` | current env-name template | current variable names | secret values | VM env names |
| `docs/runbooks/oci_runtime_hardening.md` | runtime hardening plan | opt-in Postgres healthcheck, immutable-image, bind-mount, watchdog remediation | instructions that change live VM outside maintenance | VM evidence, current drift |
| `docs/runbooks/oi_ml_shadow_sidecar.md` | OI/ML progress | sidecar image, tables, env names, scorer modes, open market-data proof gate | secret values or claims of live order routing | OI/ML sidecar deployment evidence |
| non-OCI deployment docs | reference only | non-current banner | production instructions | old runbooks |

## Delete / Archive Decisions

| Path | Action | Reason | Replacement / canonical reference |
|---|---|---|---|
| none | DELETE | No files were deleted in this pass; stale docs were rewritten or marked non-current to preserve history without presenting them as current | `README.md`, `docs/OCI_VM_RUNTIME.md`, `docs/runbooks/oci_live_deployment.md` |
| none | ARCHIVE | No file moves were required; historical release evidence remains in `docs/release-evidence/` and backlog remains in `docs/archive/` | `docs/DOCUMENTATION_AUDIT.md` inventory |

## Remaining Documentation Risks

| Risk | Severity | Status |
|---|---|---|
| Several deep reference docs still describe target architecture more than VM-observed behavior | P2 | Marked in inventory; not operator entrypoints |
| `docs/nse-holidays.txt` was not independently validated against an official NSE source during this pass | P2 | Follow-up needed before relying on holiday automation |
| OI/ML sidecar still needs market-session FULL quote completeness evidence before promotion | P1 | Captured in `docs/runbooks/oi_ml_shadow_sidecar.md` |
| Release evidence endpoint was not captured with admin auth because the admin key was not printed or used | P2 | Endpoint unauthenticated status verified as 401 |
| Script headers under `scripts/ops/` were inventoried but not fully rewritten | P2 | README/runbook no longer depend on them as canonical |

## Validation Record

Validation commands and results are recorded in the final assistant report for
this pass. Re-run after any further documentation edit.
