# Documentation Audit

Audit date: 2026-07-03. Runtime snapshot refreshed after the 2026-06-23
EMA20-only LIVE enablement, canonical-host login repair, persistent OI/ML
sidecar dormancy, and Phoenix DB backup cron installation. On 2026-06-28, the
Docker Desktop LIVE runbook was also refreshed with local LIVE runtime evidence
against Windows PostgreSQL 18 `phoenix`. On 2026-06-29, the Vultr reverse proxy
runbook was added for the active public access path after OCI VM retirement,
HTTPS was enabled for
`app.phoenixtechnosolutions.in`, and the reverse tunnel owner was moved from a
Windows Scheduled Task to Docker sidecar `phoenix-v9-vultr-tunnel`. On
2026-07-03, kill-switch runbooks were refreshed for the deployed
`Legacy Recovery Clear` flow that resets stale legacy intraday drawdown
baselines only after broker flatness/open-order validation.

Scope: repository documentation, environment examples, Compose comments, and
operator-facing runbooks were checked against the last verified OCI VM evidence
and the active local Docker/Vultr deployment. After the 2026-06-29 OCI cutoff,
the local Docker/Vultr runbooks override historical OCI assumptions for active
operations; OCI docs remain restoration-only baseline material.

## Runtime Evidence Summary

| Area | Verified current state |
|---|---|
| Repo path | `/opt/phoenix/app` |
| Active git | `main`; verify checkout SHA and running image tags from VM release evidence for each rollout |
| Compose project | `phoenix-oci-live` |
| Compose files | `/opt/phoenix/app/docker-compose.oci-live.yml`, `/opt/phoenix/phoenix-override.yml` |
| Env file | `/opt/phoenix/phoenix-deploy.env` |
| Backend | `phoenix-oci-backend`, local Phoenix backend image verified during each rollout; cron stops it outside scheduled runtime |
| Web | `phoenix-oci-web`, local Phoenix nginx image verified from `docker ps`, healthy |
| OI/ML shadow sidecar | No container present; retained image and operator Compose use restart `no`, runner/snapshotter/backend monitoring are disabled, and historical data is preserved |
| Database | VM-local `phoenix-oci-postgres`, `postgres:16-alpine`, Compose-managed and Docker-healthy |
| Database backup | `/etc/cron.d/phoenix-postgres-backup` runs the VM-local backup script at 23:30 IST Monday-Friday; dry-run schema dump and restore-list verification passed on 2026-06-23 |
| Watchdog | `phoenix-oci-watchdog`, observe-only, no Docker socket or mounts |
| Runtime mode | `APP_ENV=production`, `TRADE_MODE=LIVE`, EMA20-only; `/health/summary` reports `HUB_AUTHORITATIVE` and `/health` reports `strategy_bridge_order_router` |
| Health endpoints | backend-local `/health`, `/ready`, `/readyz`, `/health/summary`, `/health/alerts`, `/health/mitigations`; public nginx `/health`, redacted `/readyz`, redacted `/health/summary`, JSON `/health/alerts`, JSON `/health/mitigations` |
| Frontend health rendering | Overview and Safety use authenticated `/admin/health/summary` for internal diagnostics and fall back to redacted public `/health/summary` |
| Storage | root filesystem is 183G with 45G available and 76% used; OCI Compose enables the `disk_headroom_low` alert |
| Secret model | `/run/secrets/*`; permission validator passes; docs may list names only |

Full evidence: [OCI VM Runtime Evidence](OCI_VM_RUNTIME.md).

## Documentation Inventory

| Path | Type | Current status | Action |
|---|---|---|---|
| `README.md` | operator entrypoint | CURRENT_LOCAL_RECOVERY_WITH_OCI_BASELINE | KEEP CURRENT |
| `ABOUTME.md` | plain-language summary | CURRENT_LOCAL_RECOVERY_WITH_OCI_BASELINE | KEEP CURRENT |
| `ARCHITECTURE.md` | production contract | CURRENT_LOCAL_RECOVERY_WITH_OCI_BASELINE | KEEP CURRENT |
| `docs/ENCYCLOPEDIA.md` | runtime glossary and endpoint behavior index | CURRENT_WITH_LEGACY_DRAWDOWN_RECOVERY | KEEP CURRENT |
| `docs/OCI_VM_RUNTIME.md` | historical evidence snapshot | HISTORICAL_OCI_RESTORATION_ONLY | KEEP AS ARCHIVE |
| `docs/runbooks/oci_live_deployment.md` | OCI restoration operations | HISTORICAL_OCI_RESTORATION_ONLY | KEEP AS ARCHIVE |
| `docs/runbooks/oci_runtime_hardening.md` | OCI hardening/restoration | HISTORICAL_OCI_RESTORATION_ONLY | KEEP AS ARCHIVE |
| `docs/runbooks/postgres_backup.md` | historical VM-local Postgres backup automation | HISTORICAL_OCI_RESTORATION_ONLY | KEEP AS ARCHIVE |
| `docs/runbooks/oci-live.env.example` | OCI env template | HISTORICAL_OCI_RESTORATION_ONLY | KEEP AS ARCHIVE |
| `phoenix-override.yml.example` | OCI override template | MATCHES_OCI_VM_SHAPE | KEEP CURRENT |
| `docker-compose.oci-live.yml` | base Compose manifest | CURRENT_WITH_OVERRIDE_CONTEXT | KEEP CURRENT |
| `docs/runbooks/release_evidence.md` | release evidence | MATCHES_CURRENT_ENDPOINTS | KEEP CURRENT |
| `docs/release-evidence/README.md` | release evidence folder guide | MATCHES_CURRENT_ENDPOINTS | KEEP CURRENT |
| `docs/kpis_slos.md` | observability/KPI reference | MATCHES_CURRENT_ENDPOINTS | KEEP CURRENT |
| `docs/runbooks/update_broker_credentials.md` | broker credential rotation | MATCHES_OCI_VM | KEEP CURRENT |
| `docs/runbooks/dashboard-kill-switch.md` | kill-switch dashboard playbook | CURRENT_WITH_LEGACY_RECOVERY_CLEAR | KEEP CURRENT |
| `docs/runbooks/oi_ml_shadow_sidecar.md` | OI/ML shadow sidecar | CURRENT_DORMANT | KEEP CURRENT |
| `docs/runbooks/docker_desktop_live_deployment.md` | Docker Desktop/Vultr active runtime | CURRENT_ACTIVE_LOCAL | KEEP CURRENT |
| `docs/runbooks/vultr_reverse_proxy.md` | Vultr public reverse proxy for local Phoenix | CURRENT_LOCAL_PROXY_HTTPS_ACTIVE | KEEP CURRENT |
| `docs/runbooks/cloud_run_live_deployment.md` | Cloud Run reference | ROADMAP_ONLY | KEEP WITH BANNER |
| `docs/runbooks/blue_green_cutover.md` | cutover plan | ROADMAP_ONLY_FOR_CURRENT_VM | KEEP WITH BANNER |
| `docs/runbooks/restore_drill.md` | restore drill | CURRENT_WITH_OCI_NOTE | KEEP CURRENT |
| `docs/runbooks/strategy_runtime_diagnostics.md` | strategy diagnostics | MATCHES_CURRENT_READYZ_GATES | KEEP CURRENT |
| `docs/runbooks/kill_switch.md` | kill-switch operations | MATCHES_CURRENT_LEGACY_RECOVERY_AUTHORITY | KEEP CURRENT |
| `docs/STRATEGIES.md` | strategy catalog | HISTORICAL_AND_CURRENT_REFERENCE | KEEP |
| `docs/Flowchart.md` | architecture diagrams | CURRENT_ENDPOINT_CONTEXT | KEEP |
| `docs/parameters.md` | strategy parameter reference | NEEDS_STRATEGY_OWNER_REVIEW | KEEP |
| `docs/nse-holidays.txt` | scheduler input | NEEDS_OFFICIAL_SOURCE_RECHECK | KEEP |
| `docs/archive/*` | historical archive | HISTORICAL | KEEP |

## Resolved Mismatches In This Refresh

| Prior mismatch | Current resolution |
|---|---|
| Top-level docs still described the unavailable OCI VM as the only current source of truth | `README.md`, `ABOUTME.md`, `ARCHITECTURE.md`, and `docs/ENCYCLOPEDIA.md` now distinguish the active local Docker/Vultr recovery runtime from the last verified OCI VM baseline |
| Public access for the local OCI-VM replica was not documented | `docs/runbooks/vultr_reverse_proxy.md` now records the Vultr `phoenixproxy` user, Docker tunnel sidecar, SSH reverse tunnel, nginx localhost proxy, GoDaddy DNS A record, active HTTPS state, certificate expiry, HTTP-to-HTTPS redirect, and verification steps |
| Docker Desktop local replica evidence was stale while the OCI VM was unavailable | `docs/runbooks/docker_desktop_live_deployment.md` now records the 2026-06-28 local LIVE-capable stack, Windows PostgreSQL 18 `phoenix` wiring, 36 public plus 6 `legacy_phoneix` tables, EMA20-only control-plane state, green `/readyz`, and the audited recovery/table-owner repair performed during validation |
| Docs described old local image tags as current | Current operator docs require verifying checkout SHA and running image tags from VM release evidence instead of hard-coding stale SHA examples |
| Docs described `phoenix-oci-postgres` as unmanaged and lacking health | Current docs describe the Compose-managed `vm-local-postgres` profile and healthy container evidence |
| Docs described watchdog nginx stop/start behavior as current | Current docs describe the observe-only watchdog and treat Docker socket mounts or nginx mutations as drift |
| Public `/readyz` and `/health/summary` were not distinguished from internal diagnostics | Current docs state that public nginx responses are redacted and backend-local endpoints carry full diagnostics |
| Overview assumed full internal health summary fields from the public endpoint | Current frontend and docs treat the public health summary as redacted and tolerate omitted schema, alert, watchdog, and account fields |
| Alerts/Mitigations API paths fell through to SPA HTML | Current nginx repo and host-mounted templates explicitly proxy `/health/alerts` and `/health/mitigations` as JSON |
| `/bff/health/summary` bypassed public health redaction | Direct BFF access to internal diagnostics is blocked; operator dashboards use authenticated `/admin/health/summary` |
| There was no encyclopedia page for repeated endpoint terms | `docs/ENCYCLOPEDIA.md` now defines current runtime, health surfaces, dashboard health interpretation, static routing, watchdog contract, and runbook/playbook locations |
| Runtime env examples referenced old verified local image tags | Historical OCI docs describe intended `local-<git-sha>` tags and require restoration-time verification before any reinstated rollout |
| OI/ML shadow gates allowed ambiguous promotion evidence | Current code and docs require validated LightGBM artifacts, fresh IV/Greeks/source timestamps, latest validation not `ERROR`, virtual fill/flat accounting, realized dry-run PnL, and 10 clean sessions |
| Release evidence guidance treated Docker health as sufficient wait evidence | Current release guidance requires `/readyz` trading-readiness evidence in addition to liveness |
| VM cleanup cron used a stale script path with unsafe dry-run behavior | The cron path is now documented as `/opt/phoenix/scripts/weekly-cleanup.sh`, the sync/hash check is in the hardening runbook, and regression tests assert destructive commands route through dry-run logging |
| Docker journal warnings were unclassified review noise | `docs/OCI_VM_RUNTIME.md` now classifies the 2026-06-12 BuildKit, Docker socket, security-option, image-signature, and health-check warning samples |
| Root disk headroom and disk alerting were incomplete | `docs/OCI_VM_RUNTIME.md` records the 2026-06-13 45G free/76% used root filesystem evidence; `docker-compose.oci-live.yml` enables the `disk_headroom_low` alert |
| Co-tenant workload lacked explicit runtime caps | `scripts/ops/enforce_cotenant_resource_caps.sh` applies Docker CPU, memory, swap, and PID caps idempotently; the hardening runbook installs it with cron |
| Browser login returned `Invalid Host header` for the canonical domain | `PHOENIX_DOMAIN` and optional `PHOENIX_ALLOWED_HOSTS` are now forwarded to the backend Host guard; canonical login reaches validation while malformed hosts remain blocked |
| Current docs described OI/ML as running and continuously monitored | Sidecar Compose now defaults dormant, no VM container is present, the retained image and historical data remain, backend monitoring is disabled, and reactivation requires an explicit reviewed override |
| Phoenix DB backup automation was not represented in operator docs | The root cron schedule, installed script path, local dump path, log path, retention, restore-list verification, and dry-run check are documented in the Postgres backup, OCI deployment, hardening, restore, and release-evidence runbooks |
| Kill-switch docs conflated daily-loss breaches with intraday drawdown re-trips and did not document the deployed legacy baseline reset | `docs/runbooks/kill_switch.md`, `docs/runbooks/dashboard-kill-switch.md`, `docs/runbooks/docker_desktop_live_deployment.md`, and `docs/ENCYCLOPEDIA.md` now document `Legacy Recovery Clear`, its broker-flatness/open-order validation, `baseline_reset` audit evidence, and the 2026-07-03 stale high-water-mark failure shape |

## Open Documentation-Backed Risks

| Risk | Severity | Current doc location |
|---|---|---|
| Previously exposed secret values still require rotation | P0 | `docs/OCI_VM_RUNTIME.md`, `README.md` |
| Phoenix DB backups are VM-local only and do not provide PITR/off-host recovery | P1 | `docs/runbooks/postgres_backup.md`, `docs/runbooks/restore_drill.md` |
| Local Docker Desktop LIVE-capable recovery runtime is not a durable long-term production replacement for a dedicated host | P1 | `docs/runbooks/docker_desktop_live_deployment.md`, `docs/OCI_VM_RUNTIME.md` |
| Vultr public access depends on Docker Desktop, the local Phoenix stack, and `phoenix-v9-vultr-tunnel` remaining up | P1 | `docs/runbooks/vultr_reverse_proxy.md` |
| Phoenix still shares the VM with capped unrelated workloads | P2 | `docs/OCI_VM_RUNTIME.md`, `docs/runbooks/oci_runtime_hardening.md` |
| Deployment-env backup retention and forbidden secret-like scanning remain open | P1 | `docs/OCI_VM_RUNTIME.md`, `docs/runbooks/oci_live_deployment.md` |

## Canonical Operator Map

| Canonical document | Purpose |
|---|---|
| `README.md` | concise production entrypoint and reading order |
| `docs/ENCYCLOPEDIA.md` | glossary for current runtime terms, endpoint behavior, and dashboard health interpretation |
| `docs/runbooks/docker_desktop_live_deployment.md` | active local Docker Desktop recovery runtime |
| `docs/runbooks/vultr_reverse_proxy.md` | active Vultr HTTPS proxy and tunnel sidecar |
| `docs/OCI_VM_RUNTIME.md` | last verified OCI VM evidence snapshot |
| `docs/runbooks/oci_live_deployment.md` | OCI restoration/deployment runbook |
| `ARCHITECTURE.md` | production contract with current runtime preface |
| `docs/runbooks/release_evidence.md` | approval evidence standard |
| `docs/runbooks/oci_runtime_hardening.md` | hardening state, repeatable checks, and rollback notes |
| `docs/runbooks/postgres_backup.md` | VM-local Postgres backup schedule, verification, and failure handling |
| `docs/runbooks/update_broker_credentials.md` | broker credential rotation without leaking values |

## Validation Record

Documentation guard tests should include:

```bash
pytest tests/test_oci_live_cron_hardening.py tests/test_live_secret_permissions.py tests/security/test_sprint1_security.py
```

Re-run the audit after any deployment that changes VM image tags, Compose
ownership, public health routing, secret materialization, or storage topology.
