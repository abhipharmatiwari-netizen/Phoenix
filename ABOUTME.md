# About Phoenix

Phoenix is an operator-run trading platform. Its current production deployment
is the OCI VM described in [OCI VM Runtime Evidence](docs/OCI_VM_RUNTIME.md).
This file is plain-language context only; it is not an operating runbook.

## What Is Running Today

The verified OCI VM deployment is a hub-authoritative Phoenix runtime behind
nginx:

- Latest verified VM checkout is `main` at `7060dd0`, with backend/nginx
  runtime images tagged `local-c8c80ea`.
- `phoenix-oci-backend` runs `python -m app.main`.
- `phoenix-oci-web` serves the frontend and reverse-proxies current health/API
  paths. Public `/readyz` and `/health/summary` responses are redacted.
- `phoenix-oci-postgres` is the VM-local operational Postgres database and is
  now managed by the Compose `vm-local-postgres` profile with Docker health.
- `phoenix-oci-watchdog` monitors backend `/health` in observe-only mode with
  no Docker socket or mounts.
- The backend reports `HUB_AUTHORITATIVE` mode and
  `strategy_bridge_order_router` order path through health endpoints.

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

The following material may exist in the repository but is not the current
production operating model:

- Docker Desktop LIVE deployment
- Cloud Run, GCP Secret Manager, Firestore, or BigQuery authority
- OCIR-only deployment without the VM override currently in use
- external OCI Database for PostgreSQL
- repo-stored secrets or filled env files
- local development or PAPER/SHADOW examples

## Operator Responsibility

Operators must keep `/opt/phoenix/phoenix-deploy.env`, `/run/secrets`, the local
Postgres container, the source checkout at `/opt/phoenix/app`, cron jobs, and
nginx certificates consistent with the current OCI runbook. Secret values and
private infrastructure details must be redacted from logs, docs, commits, and
screenshots.
