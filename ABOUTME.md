# About Phoenix

Phoenix is an operator-run trading platform. The current active recovery
deployment is the local Docker Desktop LIVE stack described in
[Docker Desktop LIVE Deployment](docs/runbooks/docker_desktop_live_deployment.md),
published through [Vultr Reverse Proxy For Local Phoenix](docs/runbooks/vultr_reverse_proxy.md).
The OCI VM described in [OCI VM Runtime Evidence](docs/OCI_VM_RUNTIME.md) remains
the last verified VM baseline, but it is unavailable and is not current running
evidence.

This file is plain-language context only; it is not an operating runbook. The
runtime glossary and endpoint behavior index live in
[Phoenix Encyclopedia](docs/ENCYCLOPEDIA.md).

## What Is Running Today

The active 2026-06-29 recovery deployment is:

- Windows Docker Desktop running `docker-compose.live.single.yml`.
- `phoenix-v9-backend` in `APP_ENV=production`, `TRADE_MODE=LIVE`, with
  Postgres-backed control state and broker secrets.
- `phoenix-v9-web` on `127.0.0.1:80`.
- Windows PostgreSQL 18 database `phoenix`.
- `phoenix-v9-vultr-tunnel`, a Docker sidecar that waits for nginx readiness
  and opens the reverse SSH tunnel to Vultr.
- Vultr nginx at `65.20.69.50` serving
  `https://app.phoenixtechnosolutions.in`.
- The Windows Scheduled Task tunnel is installed but disabled as fallback.

The last verified OCI VM deployment was a hub-authoritative Phoenix runtime
behind nginx:

- Latest verified VM checkout is `main` at `1d0ca01`; backend and nginx are
  running local images tagged `local-1d0ca01`.
- `phoenix-oci-backend` runs `python -m app.main`.
- `phoenix-oci-web` serves the frontend and reverse-proxies current health/API
  paths. Public `/readyz` and `/health/summary` responses are redacted, while
  `/health/alerts` and `/health/mitigations` are proxied as JSON for the
  operator screens.
  Detailed schema, watchdog, and account-count diagnostics are available to the
  logged-in console through authenticated `/admin/health/summary`.
  The Overview page must render against that redacted public summary and show
  fallback values for internal-only diagnostics that are omitted.
- `phoenix-oci-postgres` is the VM-local operational Postgres database and is
  now managed by the Compose `vm-local-postgres` profile with Docker health.
- A root cron job backs up the VM-local Phoenix database at 23:30 IST
  Monday-Friday to `/opt/phoenix/backups/postgres`, with restore-list
  verification before each dump is published.
- `phoenix-oci-watchdog` monitors backend `/health` in observe-only mode with
  no Docker socket or mounts.
- The backend reports `HUB_AUTHORITATIVE` mode and
  `strategy_bridge_order_router` order path through health endpoints.
- The backend is in `APP_ENV=production`, `TRADE_MODE=LIVE`, with EMA20 as the
  only enabled strategy for the intended LIVE account. Broker state was flat,
  the kill switch inactive, and `/readyz` green at the 2026-06-20 verification.
- No `phoenix-oi-ml-shadow` container is present. Its retained image and operator
  Compose remain with restart policy `no`; runner, snapshotter, and backend
  ingestion monitoring are disabled without deleting historical database,
  image, or log evidence.
- The canonical deployment hostname is passed into the backend Host guard so
  browser login works without weakening malformed/unapproved Host rejection.

This is not a pure OCIR/external-Postgres deployment today. The VM currently
uses local Phoenix images, a local Postgres container, and several source-file
bind mounts. The immediate hardening pass fixed readiness/redaction, secret-file
permissions, Postgres health orchestration, watchdog socket exposure, and root
disk headroom; credential rotation and co-tenant isolation remain open.

## Safety Model

Phoenix should fail closed when required control state, secret inputs, durable
stores, fresh market data, broker sync, or authorization evidence are missing.
Order placement and exits must go through the current authoritative order path;
broker snapshots and dashboard state are evidence, not control authority.

## What Is Not Current Production

The following material may exist in the repository but is not the current active
operating model:

- Cloud Run, GCP Secret Manager, Firestore, or BigQuery authority
- OCIR-only deployment without the VM override currently in use
- external OCI Database for PostgreSQL
- repo-stored secrets or filled env files
- local development or PAPER/SHADOW examples

Docker Desktop LIVE deployment is current only for the documented local
recovery mode while the OCI VM is unavailable.

## Operator Responsibility

Operators must keep the active local Docker/Vultr stack consistent with the
Docker Desktop and Vultr runbooks. For OCI restoration work, keep
`/opt/phoenix/phoenix-deploy.env`, `/run/secrets`, the VM-local Postgres
container, the source checkout at `/opt/phoenix/app`, cron jobs, database backup
evidence, and nginx certificates consistent with the OCI runbook. Secret values
and private infrastructure details must be redacted from logs, docs, commits,
and screenshots.
