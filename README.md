# Phoenix

Phoenix is an operator-run trading system. `ARCHITECTURE.md` is the production contract; this README is the short operator index for the current repo.

If any repo asset, runbook, helper script, or deployment note conflicts with `ARCHITECTURE.md`, treat the architecture document as authoritative and fix the conflicting asset before using it.

## Current Automated LIVE Contract

The recommended automated LIVE runtime is exact:

- `TRADE_MODE=LIVE`
- `ENABLE_MULTI_HUB=true`
- `USE_HUB_ROUTER=true`
- `DISABLE_STREAM_WORKER=false`
- stream worker: broker market data, ticks, bars, indicators, live marks, and strategy signal generation
- hub/router/lifecycle/account runners: order authority, idempotency, ownership, broker sync, reconciliation, lifecycle polling, and durable control-plane enforcement
- Postgres: authoritative operational store for outbox, lifecycle state, ownership, kill-switch durability, sweep/EOD state, control-plane rows, and the bundled broker-credential path
- LIVE secrets: Secret Manager, OCI Vault/Docker secrets, or Postgres-backed broker credentials; repo env files are templates only

`DISABLE_STREAM_WORKER=true` is not an automated LIVE profile unless an approved replacement market-data/bar/indicator/strategy plane exists and is wired end to end.

## Current Deployment Surfaces

| Surface | Status | Canonical doc |
|---|---|---|
| Docker Desktop single-stack Compose | Bundled local LIVE implementation | [Docker Desktop LIVE Deployment](docs/runbooks/docker_desktop_live_deployment.md) |
| OCI Compose | Repo-tracked cloud Compose implementation; requires deployment-specific evidence | [OCI LIVE Deployment](docs/runbooks/oci_live_deployment.md) |
| Cloud Run | Roadmap/reference only; not approved for go-live | [Cloud Run reference](docs/runbooks/cloud_run_live_deployment.md) |
| `docker.env` and `cloudrun.env` | Local/reference templates only | This README and file comments |

No manifest is proof of LIVE readiness by itself. Approval depends on the backend container's effective environment, `/readyz`, startup/reconciliation logs, and release evidence.

## Required Operator Evidence

Before any LIVE approval, capture and review:

- rendered Compose or deployment spec
- `docker compose ps` or platform equivalent
- effective backend environment proving the LIVE tuple
- `/health/summary` and `/readyz`
- `/admin/release-evidence` using `X-Admin-Key`
- backend logs showing startup validation, schema guard, leader lease, recovery/reconciliation, runner startup, stream-worker startup, and balance sync readiness

See [LIVE Release Evidence](docs/runbooks/release_evidence.md).

## Documentation Map

| Document | Role |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Authoritative production contract |
| [ABOUTME.md](ABOUTME.md) | Plain-language, non-authoritative summary |
| [Docker Desktop LIVE Deployment](docs/runbooks/docker_desktop_live_deployment.md) | Local Compose operator runbook |
| [OCI LIVE Deployment](docs/runbooks/oci_live_deployment.md) | OCI Compose operator runbook |
| [LIVE Release Evidence](docs/runbooks/release_evidence.md) | Approval evidence standard |
| [Capital limits configuration](docs/runbooks/capital_limits_configuration.md) | `CAPITAL_LIMITS_JSON` and margin policy |
| [Broker credential update](docs/runbooks/update_broker_credentials.md) | Postgres `broker_credentials` rotation |
| [Blue/Green cutover](docs/runbooks/blue_green_cutover.md) | Controlled writer handoff |
| [Restore drill](docs/runbooks/restore_drill.md) | Backup/restore validation |
| [Break-glass flatten](docs/runbooks/break_glass_flatten.md) | Emergency exit path; not approved for LIVE unless step-up token issuance is available |
| [Orphan review resolution](docs/runbooks/resolve_orphan_review.md) | `ORPHAN_REVIEW` operator workflow |
| [Kill switch](docs/runbooks/kill_switch.md) | Trip, clear, and rearm workflow |
| [Runtime KPIs and SLO targets](docs/kpis_slos.md) | Day-1 monitor set and alert thresholds |
| [Strategy runtime diagnostics](docs/runbooks/strategy_runtime_diagnostics.md) | Stream/strategy startup diagnostics |

## Repository Layout

```text
app/                              Backend service and trading runtime
frontend/                         React operations console
nginx/                            Reverse proxy and frontend image config
migrations/                       SQL migrations and bootstrap assets
scripts/                          Operator and release utility scripts
scripts/replay/                   Deterministic replay harness and optimizer
tests/                            Pytest suite
docs/runbooks/                    Current operator procedures and references
Dockerfile                        Backend image build
docker-compose.live.single.yml    Docker/Desktop LIVE manifest
docker-compose.oci-live.yml       OCI Compose manifest
phoenix-override.yml.example      OCI override template
docker.env                        Local SHADOW/dev template only
cloudrun.env                      Cloud Run reference template only
```

## Not Current LIVE Authority

The following are not authoritative LIVE stores or current go-live paths:

- Firestore-backed control-plane or broker-secret authority
- BigQuery or CSV as live operational authority
- root env files as LIVE secret sources
- legacy-authoritative LIVE mode
- Cloud Run deployment
- stale multi-file Docker Compose profiles not present in this repo

Build clean promotion artifacts with `python scripts/build_release_artifact.py --output release/phoenix-live-source.zip`.
