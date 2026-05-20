# Phoenix

Phoenix is currently operated from an OCI VM. The running OCI VM is the only
source of truth for production documentation; repo manifests and historical
runbooks are secondary evidence only when they match that VM.

Last verified against the VM: 2026-05-20 16:02 UTC.
OI/ML shadow sidecar deployment was verified on 2026-05-20 00:21 IST.

## Current OCI VM State

| Area | Verified state |
|---|---|
| Host | `phoenix-vm` |
| Repo checkout | `/opt/phoenix/app` |
| Git state on VM | branch `main`, commit `349d55f`, untracked `docker-compose.oci-postgres.yml` |
| Compose project | `phoenix-oci-live` |
| Compose files in use | `/opt/phoenix/app/docker-compose.oci-live.yml`, `/opt/phoenix/phoenix-override.yml` |
| Env file in use | `/opt/phoenix/phoenix-deploy.env` |
| Backend container | `phoenix-oci-backend`, image `phoenix-local-backend:local-349d55f`, healthy |
| Web container | `phoenix-oci-web`, image `phoenix-local-nginx:local-349d55f`, healthy |
| Database | VM-local `phoenix-oci-postgres` container, `postgres:16-alpine`, no Docker healthcheck |
| Watchdog | `phoenix-oci-watchdog`, `docker:cli`; actively stops/starts nginx when backend health fails |
| OI/ML shadow sidecar | `phoenix-oi-ml-shadow`, image `phoenix-oi-ml-shadow:oi-ml-shadow-29c24f0`, dry-run only |
| Backend command | `python -m app.main` |
| Public backend exposure | backend port `8080` is container-only; nginx exposes host ports `80` and `8443` |
| Health checks | backend container: `/health`, `/ready`, `/readyz`, `/health/summary`; nginx/host: `/health`, `/readyz` |
| Runtime mode evidence | `/health` reports `order_path=strategy_bridge_order_router`; `/health/summary` reports `operating_mode=HUB_AUTHORITATIVE` |
| Secrets | secret files under `/run/secrets`; required names are documented, values must never be copied into git |

Current drift that operators must not normalize:

- The VM is not running OCIR images; it is running local images tagged `local-349d55f`.
- The VM is not using an external OCI Database for PostgreSQL; it is using a VM-local Postgres container.
- The backend has source-file bind mounts from `/opt/phoenix/app` into the container.
- `CONTROL_PLANE_PG_SSLMODE=prefer` and `LIVE_PG_SSL_SKIP_CHECK=true` are present because the DB is local to the VM.
- `phoenix-oci-postgres` is not part of the labelled Compose project and has no healthcheck.

See [OCI VM Runtime Evidence](docs/OCI_VM_RUNTIME.md) for the evidence table.

## Operator Reading Order

1. [OCI VM Runtime Evidence](docs/OCI_VM_RUNTIME.md)
2. [OCI LIVE Deployment Runbook](docs/runbooks/oci_live_deployment.md)
3. [Architecture Contract](ARCHITECTURE.md)
4. [Documentation Audit](docs/DOCUMENTATION_AUDIT.md)
5. [Release Evidence](docs/runbooks/release_evidence.md)
6. [OCI Runtime Hardening](docs/runbooks/oci_runtime_hardening.md)
7. [Strategy Runtime Diagnostics](docs/runbooks/strategy_runtime_diagnostics.md)
8. [OI/ML Shadow Sidecar](docs/runbooks/oi_ml_shadow_sidecar.md)
9. [Kill Switch](docs/runbooks/kill_switch.md)
10. [Broker Credential Update](docs/runbooks/update_broker_credentials.md)
11. [Restore Drill](docs/runbooks/restore_drill.md)

Docker Desktop, Cloud Run, GCP, Firestore, BigQuery, and local development
material are not current production operating models unless a future VM audit
proves otherwise.

## Safe VM Commands

Run these only after connecting to the OCI VM as the operator user. Redact
secret values and private host details before sharing output.

```bash
cd /opt/phoenix/app

docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
docker inspect phoenix-oci-backend --format '{{json .Config.Image}} {{json .Config.Cmd}} {{json .HostConfig.RestartPolicy}}'
docker inspect phoenix-oci-backend --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -E 's/=.*$/=<REDACTED>/'

docker exec phoenix-oci-backend curl -sS http://localhost:8080/readyz
curl -k -sS https://localhost:8443/readyz

docker logs --tail=300 phoenix-oci-backend
docker logs --tail=120 phoenix-oci-watchdog
```

Do not restart containers, run migrations, update credentials, flatten positions,
or place/cancel/modify orders unless the relevant current OCI runbook explicitly
requires that action and the operator has approved it.

## Repository Layout

```text
app/                              Backend service and trading runtime
frontend/                         React operations console
nginx/                            Reverse proxy and frontend image config
migrations/                       SQL migrations and bootstrap assets
scripts/                          Operator and release utility scripts
tests/                            Pytest suite
docs/OCI_VM_RUNTIME.md            Current VM evidence snapshot
docs/runbooks/oci_live_deployment.md Current OCI VM operator runbook
docs/runbooks/oi_ml_shadow_sidecar.md OI/ML shadow sidecar progress and gates
docker-compose.oci-live.yml       Base Compose file used with the VM override
phoenix-override.yml.example      Template mirroring the current VM override shape
```

## Secret Rule

Never commit or paste real values for broker credentials, database passwords,
admin keys, JWT/session/HMAC secrets, TOTP/PIN values, tokens, private IPs, or
OCI identifiers. Documentation may list required variable names only.
