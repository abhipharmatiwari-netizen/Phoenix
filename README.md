# Phoenix v9

Phoenix v9 is an operator-run trading system. This README reflects the **current recommended automated LIVE runtime** defined in [`ARCHITECTURE.md`](ARCHITECTURE.md) and the Docker/Desktop implementation assets bundled here.

If any repo asset, runbook, or helper script conflicts with `ARCHITECTURE.md`, the architecture document is authoritative.

---

## Current recommended automated LIVE runtime

The current recommended automated LIVE runtime is the following exact operating model:

- `TRADE_MODE=LIVE`
- `ENABLE_MULTI_HUB=true`
- `USE_HUB_ROUTER=true`
- `DISABLE_STREAM_WORKER=false`
- **stream worker** handles broker market-data session, ticks, bar building, indicator updates, live marks, and strategy signal generation
- **hub/router/lifecycle/account runners** remain authoritative for order submission, idempotency, ownership, broker sync, reconciliation, lifecycle polling, and durable control-plane enforcement
- Postgres is the authoritative operational store for outbox, lifecycle state, ownership, kill-switch durability, sweep state, EOD state, control-plane configuration, and the current bundled broker-credential path
- LIVE secrets must be sourced from **Secret Manager or Postgres**; short-lived injected environment variables may transport those values at runtime, but repo env files are not approved secret sources
- release readiness is judged from the **backend container's effective runtime state**, health, and reconciliation evidence, not from the launching shell alone

### Why `DISABLE_STREAM_WORKER=false` matters

In the current recommended automated LIVE profile, fresh ticks, bars, indicators, and open mark-to-market depend on the stream-worker market-data path. Broker position/order polling is still required, but broker sync alone does not satisfy the automated LIVE baseline for signal generation or fresh open PnL.

`DISABLE_STREAM_WORKER=true` is valid only for operator/control-plane, reconciliation, or manual-supervision mode unless an approved replacement market-data plane exists and is wired end to end.

---

## What this aligned bundle contains

This bundle includes a Docker/Desktop implementation example of the current recommended automated LIVE runtime:

- [`docker-compose.live.single.yml`](docker-compose.live.single.yml) — single-file Docker manifest updated to the stream-enabled recommended LIVE runtime
- [`docs/runbooks/docker_desktop_live_deployment.md`](docs/runbooks/docker_desktop_live_deployment.md) — operator runbook for that manifest
- [`start-docker-secretstore.ps1`](start-docker-secretstore.ps1) — optional helper that exports runtime variables into the current PowerShell session before Compose starts

### Secret-source rule

The helper script above is an operator convenience only. It must not be read as a change to the architecture rule that LIVE secrets come from **Secret Manager or Postgres**. If you use a local host-side vault or PowerShell helper, treat it as a transport step for values that remain governed by the approved LIVE secret-management process.

---

## What is not the current automated LIVE baseline

The following are **not** the current recommended automated LIVE baseline:

- legacy-authoritative LIVE mode
- `DISABLE_STREAM_WORKER=true` for automated LIVE without an approved replacement market-data/bar/indicator plane
- repo-managed secret values committed to git or placed in `.env` / `_LEGACY_ENV_REFERENCE.env`
- treating CSV, BigQuery, dashboard state, JSON helpers, or in-memory state as authoritative live control state
- Cloud Run as the current default production path
- Firestore-backed broker secrets as the current default production path
- BigQuery as an authoritative operational store

Those items may exist as reference material, compatibility assets, or roadmap work, but they are not the current recommended automated LIVE contract.

---

## Bundled Docker/Desktop implementation path

Use the bundled runbook:

- [Docker Desktop LIVE Deployment](docs/runbooks/docker_desktop_live_deployment.md)

Required control-plane data before go-live:

- tenant row exists for the target tenant
- broker account row exists for the target `broker_account_id`
- subscription / strategy configuration rows exist for the target tenant and broker account
- `broker_credentials` row exists for the target `broker_account_id` when using the bundled Postgres-backed broker-secret path

The bundled Docker/Desktop path builds and starts Phoenix in LIVE mode with the stream-enabled recommended runtime. Use the runbook for the exact command, verification steps, and release evidence.

---

## Primary surfaces

| Surface | Purpose |
|---|---|
| `http://localhost` | Browser-facing operations console through nginx |
| `http://localhost/health` | Liveness probe |
| `http://localhost/health/summary` | Startup and dependency summary |

### Exposure rule

For the bundled Docker/Desktop path, nginx is the browser-facing entrypoint. Direct public exposure of the backend container is not part of this runbook.

---

## Documentation map

| Document | Role |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Authoritative production contract |
| [`ABOUTME.md`](ABOUTME.md) | Plain-language operational summary |
| [Docker Desktop LIVE Deployment](docs/runbooks/docker_desktop_live_deployment.md) | Bundled Docker/Desktop implementation runbook |
| [Capital limits configuration](docs/runbooks/capital_limits_configuration.md) | Per-account notional/margin limits and `CAPITAL_LIMITS_JSON` format |
| [Broker credential update runbook](docs/runbooks/update_broker_credentials.md) | How to rotate SmartAPI credentials in Postgres |
| [Blue/Green cutover](docs/runbooks/blue_green_cutover.md) | Controlled writer handoff |
| [Restore drill](docs/runbooks/restore_drill.md) | Backup / restore validation |
| [Break-glass flatten](docs/runbooks/break_glass_flatten.md) | Emergency contract exit via admin route |
| [Orphan review resolution](docs/runbooks/resolve_orphan_review.md) | How to resolve ORPHAN_REVIEW position states |
| [Kill switch reference](docs/runbooks/kill_switch.md) | Kill switch detection and clear procedures |
| [Cloud Run deployment reference](docs/runbooks/cloud_run_live_deployment.md) | Reference / roadmap material only |

---

## Repository layout

```text
app/                              Backend service and trading runtime
frontend/                         React operations console
nginx/                            Reverse proxy and frontend image config
migrations/                       SQL migrations and bootstrap assets
scripts/                          Operator utility scripts (generate_sbom.py, replay engine, etc.)
scripts/replay/                   Bar-by-bar deterministic replay harness and optimizer
tests/                            Test suite (1551 tests)
docs/runbooks/                    Operator procedures
.github/workflows/                CI — security scan (gitleaks, pip-audit, SBOM generation)
Dockerfile                        Backend image build (multi-stage, non-root, healthcheck built in)
docker.env                        Local SHADOW/dev profile only — not used by production compose
docker-compose.live.single.yml    Bundled Docker/Desktop LIVE manifest
start-docker-secretstore.ps1      PowerShell helper — loads SecretStore secrets into session before Compose
start-docker-secretstore.cmd      Convenience launcher for the PowerShell helper from Windows Explorer
```

---

## Production hardening guards

The following guards are wired directly into [`docker-compose.live.single.yml`](docker-compose.live.single.yml) and enforced at startup. They cannot be accidentally bypassed by the host shell environment.

| Guard | Env var | What it enforces |
|---|---|---|
| Stack-lock | `REQUIRE_LIVE_TRADE_MODE=true` | Startup validator hard-fails if `TRADE_MODE != LIVE`; prevents SHADOW/PAPER deployment of the LIVE manifest |
| Broker schema check | `BROKER_SCHEMA_CHECK_MODE=strict` | Angel One balance/order/position API responses validated at every sync cycle; malformed shapes rejected at the integration boundary |
| Risk state isolation | `RISK_STATE_PATH=/app/state/risk_positions.json` | Risk restart-helper stored on the `/app/state` volume (separate from logs); survives log rotation |
| Selector staleness | IST-date guard in `StrategySelector` | Prior-day selection state evicted on first bar after IST midnight; prevents regime/strategy state carrying over between trading days |
| Sync freshness | `/readyz` sync-age gate | Returns 503 when position or orders sync age exceeds 2× the configured sync interval |
| Unroutable exclusion | Runtime route validation at stream startup | Attached strategies with no routing entry are dropped before dispatch; a clean Docker/Desktop LIVE start should not emit `strategy.unroutable` |

## Capital risk configuration

Production limits are pinned explicitly in [`docker-compose.live.single.yml`](docker-compose.live.single.yml):

| Env var | Production value | What it controls |
|---|---|---|
| `CAPITAL_MARGIN_CHECK_MODE` | `enforce` | Blocks orders when estimated margin > available balance |
| `CAPITAL_MARGIN_SHORT_OPTION_PER_LOT` | 2,00,000 | Per-lot margin floor for short CE/PE SELL orders |
| `CAPITAL_MARGIN_FUTURES_PER_LOT` | 2,00,000 | Per-lot margin floor for futures orders |
| `CAPITAL_MARGIN_FUTURES_RATE` | 0.12 | Fraction of futures notional used as margin estimate |
| `CAPITAL_LIMITS_JSON` | required per account | Per-account notional/exposure limits; `TRADE_MODE=LIVE` rejects empty `{}` unless an explicit audited exception is set |

The Docker/Desktop helper derives a `tenant_id:broker_account_id` entry at the 5L/10L baseline when no operator override is supplied.
Set `CAPITAL_LIMITS_JSON` explicitly for funded accounts before LIVE use.
See the [capital limits runbook](docs/runbooks/capital_limits_configuration.md) for the full override format.

> `CAPITAL_MARGIN_CHECK_MODE=off` is auto-upgraded to `enforce` whenever `TRADE_MODE=LIVE`
> even if not explicitly set — but the compose file sets it explicitly for full auditability.

---

## Non-LIVE usage

The repo may still contain local or compatibility assets such as `docker.env`, `_LEGACY_ENV_REFERENCE.env`, `docker-compose.live.yml`, `docker-compose.postgres.override.yml`, and `cloudrun.env`. Those assets are not proof of LIVE readiness by themselves. LIVE approval depends on conformance to [`ARCHITECTURE.md`](ARCHITECTURE.md), the effective runtime environment, and release evidence.

Clean promotion artifacts should be produced with `scripts/build_release_artifact.py`. The builder now works even from a source snapshot without `.git`, and it excludes runtime injection files such as `*.env.runtime`, logs, caches, and tests from the release zip.
