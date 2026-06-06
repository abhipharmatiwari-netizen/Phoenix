# Documentation Audit

Audit date: 2026-06-06. Runtime snapshot refreshed after the OCI LIVE hardening
deploy, frontend static asset redeploy, redacted-health Overview fix,
Alerts/Mitigations route fix, authenticated health-summary fix, and backend restart, with VM verification at
14:19 UTC.

Scope: repository documentation, environment examples, Compose comments, and
operator-facing runbooks were checked against the running OCI VM. The OCI VM
overrides repo docs and historical plans when there is a conflict.

## Runtime Evidence Summary

| Area | Verified current state |
|---|---|
| Repo path | `/opt/phoenix/app` |
| Active git | `main` at `4ba598f...`; deploy env image tag `local-4ba598f` |
| Compose project | `phoenix-oci-live` |
| Compose files | `/opt/phoenix/app/docker-compose.oci-live.yml`, `/opt/phoenix/phoenix-override.yml` |
| Env file | `/opt/phoenix/phoenix-deploy.env` |
| Backend | `phoenix-oci-backend`, `phoenix-local-backend:local-4ba598f`, healthy |
| Web | `phoenix-oci-web`, `phoenix-local-nginx:local-4ba598f`, healthy |
| Database | VM-local `phoenix-oci-postgres`, `postgres:16-alpine`, Compose-managed and Docker-healthy |
| Watchdog | `phoenix-oci-watchdog`, observe-only, no Docker socket or mounts |
| Runtime mode | `/health/summary` reports `HUB_AUTHORITATIVE`; `/health` reports `strategy_bridge_order_router` |
| Health endpoints | backend-local `/health`, `/ready`, `/readyz`, `/health/summary`, `/health/alerts`, `/health/mitigations`; public nginx `/health`, redacted `/readyz`, redacted `/health/summary`, JSON `/health/alerts`, JSON `/health/mitigations` |
| Frontend health rendering | Overview and Safety use authenticated `/admin/health/summary` for internal diagnostics and fall back to redacted public `/health/summary` |
| Storage | root filesystem expanded; latest evidence showed 63% used |
| Secret model | `/run/secrets/*`; permission validator passes; docs may list names only |

Full evidence: [OCI VM Runtime Evidence](OCI_VM_RUNTIME.md).

## Documentation Inventory

| Path | Type | OCI VM match status | Action |
|---|---|---|---|
| `README.md` | operator entrypoint | MATCHES_OCI_VM | KEEP CURRENT |
| `ABOUTME.md` | plain-language summary | MATCHES_OCI_VM | KEEP CURRENT |
| `ARCHITECTURE.md` | production contract | MATCHES_OCI_VM_PREFACE | KEEP CURRENT |
| `docs/OCI_VM_RUNTIME.md` | evidence snapshot | MATCHES_OCI_VM | KEEP CURRENT |
| `docs/runbooks/oci_live_deployment.md` | OCI operations | MATCHES_OCI_VM | KEEP CURRENT |
| `docs/runbooks/oci_runtime_hardening.md` | runtime hardening | MATCHES_OCI_VM | KEEP CURRENT |
| `docs/runbooks/oci-live.env.example` | OCI env template | MATCHES_OCI_VM | KEEP CURRENT |
| `phoenix-override.yml.example` | OCI override template | MATCHES_OCI_VM_SHAPE | KEEP CURRENT |
| `docker-compose.oci-live.yml` | base Compose manifest | CURRENT_WITH_OVERRIDE_CONTEXT | KEEP CURRENT |
| `docs/runbooks/release_evidence.md` | release evidence | MATCHES_CURRENT_ENDPOINTS | KEEP CURRENT |
| `docs/release-evidence/README.md` | release evidence folder guide | MATCHES_CURRENT_ENDPOINTS | KEEP CURRENT |
| `docs/kpis_slos.md` | observability/KPI reference | MATCHES_CURRENT_ENDPOINTS | KEEP CURRENT |
| `docs/runbooks/update_broker_credentials.md` | broker credential rotation | MATCHES_OCI_VM | KEEP CURRENT |
| `docs/runbooks/oi_ml_shadow_sidecar.md` | OI/ML shadow sidecar | CURRENT_FOR_SIDECAR | KEEP CURRENT |
| `docs/runbooks/docker_desktop_live_deployment.md` | Docker Desktop reference | NON_CURRENT_PRODUCTION | KEEP WITH BANNER |
| `docs/runbooks/cloud_run_live_deployment.md` | Cloud Run reference | ROADMAP_ONLY | KEEP WITH BANNER |
| `docs/runbooks/blue_green_cutover.md` | cutover plan | ROADMAP_ONLY_FOR_CURRENT_VM | KEEP WITH BANNER |
| `docs/runbooks/restore_drill.md` | restore drill | CURRENT_WITH_OCI_NOTE | KEEP CURRENT |
| `docs/runbooks/strategy_runtime_diagnostics.md` | strategy diagnostics | MATCHES_CURRENT_READYZ_GATES | KEEP CURRENT |
| `docs/runbooks/kill_switch.md` | kill-switch operations | MATCHES_CURRENT_AUTHORITY | KEEP CURRENT |
| `docs/STRATEGIES.md` | strategy catalog | HISTORICAL_AND_CURRENT_REFERENCE | KEEP |
| `docs/Flowchart.md` | architecture diagrams | CURRENT_ENDPOINT_CONTEXT | KEEP |
| `docs/parameters.md` | strategy parameter reference | NEEDS_STRATEGY_OWNER_REVIEW | KEEP |
| `docs/nse-holidays.txt` | scheduler input | NEEDS_OFFICIAL_SOURCE_RECHECK | KEEP |
| `docs/archive/*` | historical archive | HISTORICAL | KEEP |

## Resolved Mismatches In This Refresh

| Prior mismatch | Current resolution |
|---|---|
| Docs described `local-e7f1e29` as current | Current operator docs now reference VM checkout `4ba598f` and backend/nginx images tagged `local-4ba598f` |
| Docs described `phoenix-oci-postgres` as unmanaged and lacking health | Current docs describe the Compose-managed `vm-local-postgres` profile and healthy container evidence |
| Docs described watchdog nginx stop/start behavior as current | Current docs describe the observe-only watchdog and treat Docker socket mounts or nginx mutations as drift |
| Public `/readyz` and `/health/summary` were not distinguished from internal diagnostics | Current docs state that public nginx responses are redacted and backend-local endpoints carry full diagnostics |
| Overview assumed full internal health summary fields from the public endpoint | Current frontend and docs treat the public health summary as redacted and tolerate omitted schema, alert, watchdog, and account fields |
| Alerts/Mitigations API paths fell through to SPA HTML | Current nginx repo and host-mounted templates explicitly proxy `/health/alerts` and `/health/mitigations` as JSON |
| `/bff/health/summary` bypassed public health redaction | Direct BFF access to internal diagnostics is blocked; operator dashboards use authenticated `/admin/health/summary` |
| Runtime env examples referenced old verified local image tags | Current OCI env template references the `local-4ba598f` deploy tag |
| Release evidence guidance treated Docker health as sufficient wait evidence | Current release guidance requires `/readyz` trading-readiness evidence in addition to liveness |

## Open Documentation-Backed Risks

| Risk | Severity | Current doc location |
|---|---|---|
| Previously exposed secret values still require rotation | P0 | `docs/OCI_VM_RUNTIME.md`, `README.md` |
| Phoenix still shares the VM with unrelated public workloads | P1 | `docs/OCI_VM_RUNTIME.md`, `README.md`, `ARCHITECTURE.md` |
| Disk alerting and retention policy are not complete | P1 | `docs/OCI_VM_RUNTIME.md`, `docs/runbooks/oci_runtime_hardening.md` |
| Stale artifact and journal-warning cleanup needs a focused pass | P2 | `docs/OCI_VM_RUNTIME.md` |
| Deployment-env backup retention and forbidden secret-like scanning remain open | P1 | `docs/OCI_VM_RUNTIME.md`, `docs/runbooks/oci_live_deployment.md` |

## Canonical Operator Map

| Canonical document | Purpose |
|---|---|
| `README.md` | concise production entrypoint and reading order |
| `docs/OCI_VM_RUNTIME.md` | current VM evidence snapshot |
| `docs/runbooks/oci_live_deployment.md` | executable current OCI runbook |
| `ARCHITECTURE.md` | production contract with current runtime preface |
| `docs/runbooks/release_evidence.md` | approval evidence standard |
| `docs/runbooks/oci_runtime_hardening.md` | hardening state, repeatable checks, and rollback notes |
| `docs/runbooks/update_broker_credentials.md` | broker credential rotation without leaking values |

## Validation Record

Documentation guard tests should include:

```bash
pytest tests/test_oci_live_cron_hardening.py tests/test_live_secret_permissions.py tests/security/test_sprint1_security.py
```

Re-run the audit after any deployment that changes VM image tags, Compose
ownership, public health routing, secret materialization, or storage topology.
