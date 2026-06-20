# Phoenix Encyclopedia

Last updated: 2026-06-20.

This is the quick-reference index for the current Phoenix OCI VM runtime. It
explains terms and endpoint behavior that appear across the README,
architecture document, runbooks, and operator playbooks.

## Current Runtime

| Term | Current meaning |
|---|---|
| OCI VM | Current production source of truth for Phoenix runtime evidence. |
| VM checkout | `/opt/phoenix/app`, branch `main`; verify the checkout SHA and running image tags during each rollout. |
| Deploy image tag | Verify with `docker ps --filter name=phoenix --format '{{.Names}} {{.Image}}'`; backend/nginx use local image tags on the current VM. |
| Backend | `phoenix-oci-backend`, running `python -m app.main`. |
| Web | `phoenix-oci-web`, nginx frontend and reverse proxy. |
| Database | `phoenix-oci-postgres`, VM-local Postgres container managed by the `vm-local-postgres` Compose profile. |
| Watchdog | `phoenix-oci-watchdog`, observe-only Docker CLI sidecar with no Docker socket or mounts. |
| LIVE strategy authority | EMA20-only for the intended Angel account; `TRADE_MODE=LIVE`, one active strategy per underlying, flat broker/ownership state and green readiness were verified on 2026-06-20. |
| OI/ML sidecar | `phoenix-oi-ml-shadow`, retained but dormant and outside the live order authority path. Container stopped, restart `no`, runner/snapshotter/health monitoring disabled; data, image, and logs preserved. |
| Host allow-list | The canonical deployment domain is passed to the backend. Approved browser login works; malformed or unapproved Host values are rejected before protected routes. |

## Health And Readiness Surfaces

| Surface | Audience | Current behavior |
|---|---|---|
| Backend-local `/health` | Operator shell inside backend/nginx path | Liveness and order-path evidence. |
| Backend-local `/readyz` | Operator shell inside backend container | Full trading-readiness gate. Must be green before automated LIVE entries resume. |
| Backend-local `/health/summary` | Operator shell inside backend container | Full startup/dependency summary, including internal diagnostics. |
| Public nginx `/readyz` | Browser/public probe | Redacted readiness. It proves reachability, not full diagnostics. |
| Public nginx `/health/summary` | Browser/public probe | Redacted summary. Internal schema, watchdog, and account-count fields may be omitted. |
| Public nginx `/health/alerts` | Dashboard Alerts page and probes | JSON alert-rule payload; must not fall through to SPA HTML. |
| Public nginx `/health/mitigations` | Dashboard Mitigations page and probes | JSON mitigation payload; must not fall through to SPA HTML. |
| Authenticated `/admin/health/summary` | Logged-in operator console | Internal schema, watchdog, tracked-account, and readiness details. |
| Direct `/bff/health/summary`, `/bff/readyz`, `/bff/dashboard/status` | Not supported | Blocked with 404 so the BFF cannot bypass public redaction. |

## Dashboard Health Interpretation

The Overview and Safety pages must prefer authenticated
`/admin/health/summary` for operator-only diagnostics and fall back to redacted
public `/health/summary` when authentication is unavailable.

If Schema Status, Tracked Accounts, or Watchdog appears as `Unknown` from a
public or unauthenticated view, first verify the authenticated admin summary
before treating it as a runtime failure. In the latest verified VM probe, the
authenticated summary reported `schema_status=ok`,
`tracked_account_count=2`, `watchdog_running=true`, and `status=ok`.

If the authenticated admin summary also reports schema failure, zero tracked
accounts, or watchdog stopped, treat that as a real operational issue and use
the OCI live deployment and strategy diagnostics runbooks.

## Static Asset And SPA Routing

The nginx container must serve current frontend assets directly:

- `/manifest.json`, `/favicon.svg`, and `/favicon.ico` are static assets.
- Existing `/static/*` files return JavaScript, CSS, or media with the correct
  content type.
- Stale `/static/*` requests return 404 instead of `index.html`.
- SPA fallback is only for application routes such as `/alerts`, `/mitigations`,
  `/positions`, and `/safety`.
- React runtime failures render a visible "Phoenix could not render" recovery
  screen instead of a blank root.

## Watchdog Contract

`phoenix-oci-watchdog` is not a remediation controller. It should poll backend
`/health` and log failure/recovery counts only. It must not mount the Docker
socket, stop nginx, start nginx, restart backend, or mutate host paths. If logs
or `docker inspect` show those capabilities, treat the VM as running stale
watchdog wiring and recreate it through `scripts/ops/recreate_oci_watchdog.sh`
during an approved maintenance window.

## OI/ML Shadow Contract

The OI/ML sidecar never submits broker orders. `constant` scoring is
connectivity-only and requires an explicit smoke override. Promotion evidence
requires LightGBM model artifacts with a passed validation report, candidate
quotes with source timestamps, IV, and Greeks, a latest validation report that is
not `ERROR`, and dry-run lifecycle rows that progress through staged, virtual
filled, virtual exited, and flat with realized paper PnL.

## Runbooks And Playbooks

This repo does not have a separate `docs/playbooks/` directory. Operator
playbooks are embedded in the runbooks, especially:

- `docs/runbooks/oci_live_deployment.md`
- `docs/runbooks/release_evidence.md`
- `docs/runbooks/oci_runtime_hardening.md`
- `docs/runbooks/strategy_runtime_diagnostics.md`
- `docs/runbooks/dashboard-kill-switch.md`
- `docs/runbooks/kill_switch.md`
- `docs/runbooks/restore_drill.md`

When a historical runbook conflicts with the current OCI VM evidence, the VM
evidence wins and the runbook must be corrected.

## Non-Current Production Material

Docker Desktop, Cloud Run, GCP Secret Manager, Firestore, BigQuery authority,
OCIR-only deployment, and external OCI Database for PostgreSQL are not the
current production operating model unless a future VM audit proves they are
active.
