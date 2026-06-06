# Phoenix

Phoenix is currently operated from an OCI VM. The running OCI VM is the only
source of truth for production documentation; repo manifests and historical
runbooks are secondary evidence only when they match that VM.

Last verified against the VM: 2026-06-06 14:19 UTC.
OI/ML shadow sidecar deployment was verified on 2026-05-23 00:12 IST.

## Current OCI VM State

| Area | Verified state |
|---|---|
| Host | `phoenix-vm` |
| Repo checkout | `/opt/phoenix/app` |
| Git state on VM | branch `main`, commit `4ba598f`; deploy env image tag `local-4ba598f` |
| Compose project | `phoenix-oci-live` |
| Compose files in use | `/opt/phoenix/app/docker-compose.oci-live.yml`, `/opt/phoenix/phoenix-override.yml` |
| Env file in use | `/opt/phoenix/phoenix-deploy.env` |
| Backend container | `phoenix-oci-backend`, image `phoenix-local-backend:local-4ba598f`, healthy |
| Web container | `phoenix-oci-web`, image `phoenix-local-nginx:local-4ba598f`, healthy |
| Database | VM-local `phoenix-oci-postgres` container, `postgres:16-alpine`, Compose-managed and Docker-healthy |
| Watchdog | `phoenix-oci-watchdog`, `docker:cli`; observe-only, no Docker socket or mounts |
| OI/ML shadow sidecar | `phoenix-oi-ml-shadow`, image `phoenix-oi-ml-shadow:oi-ml-shadow-bd999cd`, dry-run only |
| Backend command | `python -m app.main` |
| Public backend exposure | backend port `8080` is container-only; nginx exposes host ports `80` and `8443` |
| Health checks | backend container: `/health`, `/ready`, `/readyz`, `/health/summary`, `/health/alerts`, `/health/mitigations`; nginx/host: `/health`, redacted `/readyz`, redacted `/health/summary`, JSON `/health/alerts`, JSON `/health/mitigations` |
| Frontend health rendering | Overview/Safety use authenticated `/admin/health/summary` for internal schema, watchdog, and account-count fields, then fall back to redacted public `/health/summary` |
| Runtime mode evidence | `/health` reports `order_path=strategy_bridge_order_router`; `/health/summary` reports `operating_mode=HUB_AUTHORITATIVE` |
| Readiness evidence | backend-local `/readyz` returned HTTP 200; public `/readyz` returned only redacted readiness and universe-health fields |
| Secrets | secret files under `/run/secrets`; deployed permission validator passes; values must never be copied into git |

Current drift that operators must not normalize:

- The VM is not running OCIR images; Phoenix backend and nginx are running local
  images tagged `local-4ba598f`.
- The VM is not using an external OCI Database for PostgreSQL; it is using a VM-local Postgres container.
- The backend has source-file bind mounts from `/opt/phoenix/app` into the container.
- `CONTROL_PLANE_PG_SSLMODE=prefer` and `LIVE_PG_SSL_SKIP_CHECK=true` are present because the DB is local to the VM.
- Phoenix still shares the VM with unrelated public workloads until issue #349 is resolved or explicitly risk-accepted.
- Previously exposed secret values still require rotation even though file permissions are now hardened.

See [OCI VM Runtime Evidence](docs/OCI_VM_RUNTIME.md) for the evidence table.

## Operator Reading Order

1. [OCI VM Runtime Evidence](docs/OCI_VM_RUNTIME.md)
2. [OCI LIVE Deployment Runbook](docs/runbooks/oci_live_deployment.md)
3. [Architecture Contract](ARCHITECTURE.md)
4. [Phoenix Encyclopedia](docs/ENCYCLOPEDIA.md)
5. [Documentation Audit](docs/DOCUMENTATION_AUDIT.md)
6. [Release Evidence](docs/runbooks/release_evidence.md)
7. [OCI Runtime Hardening](docs/runbooks/oci_runtime_hardening.md)
8. [Strategy Runtime Diagnostics](docs/runbooks/strategy_runtime_diagnostics.md)
9. [OI/ML Shadow Sidecar](docs/runbooks/oi_ml_shadow_sidecar.md)
10. [OI/ML Data Source Approval](docs/runbooks/oi_ml_data_source_approval.md)
11. [OI/ML CE Seller Rollout](docs/runbooks/oi_ml_ce_seller_rollout.md)
12. [Kill Switch](docs/runbooks/kill_switch.md)
13. [Broker Credential Update](docs/runbooks/update_broker_credentials.md)
14. [Restore Drill](docs/runbooks/restore_drill.md)

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
curl -sS http://localhost/readyz
curl -k -sS https://localhost:8443/readyz

docker logs --tail=300 phoenix-oci-backend
docker inspect phoenix-oci-postgres --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}'
docker inspect phoenix-oci-watchdog --format '{{json .Mounts}}'
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
docs/ENCYCLOPEDIA.md              Current runtime glossary and endpoint behavior
docs/runbooks/oci_live_deployment.md Current OCI VM operator runbook
docs/runbooks/oi_ml_shadow_sidecar.md OI/ML shadow sidecar progress and gates
docs/runbooks/oi_ml_data_source_approval.md OI/ML option-chain data source approval gate
docs/runbooks/oi_ml_ce_seller_rollout.md OI/ML promotion and rollback gates
docker-compose.oci-live.yml       Base Compose file used with the VM override
phoenix-override.yml.example      Template mirroring the current VM override shape
```

## Secret Rule

Never commit or paste real values for broker credentials, database passwords,
admin keys, JWT/session/HMAC secrets, TOTP/PIN values, tokens, private IPs, or
OCI identifiers. Documentation may list required variable names only.
