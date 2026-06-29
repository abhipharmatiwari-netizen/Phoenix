# Phoenix

Phoenix is currently running in a local recovery deployment because the OCI VM
is unavailable. The active runtime is a Windows Docker Desktop LIVE stack using
Windows PostgreSQL 18 database `phoenix`, exposed publicly through a Vultr HTTPS
reverse proxy and a Docker-managed reverse SSH tunnel sidecar.

The OCI VM evidence remains the last verified production VM baseline, but it is
not proof that the unavailable VM is currently running. Until OCI is restored or
a new production target is cut over, operators should treat the local
Docker/Vultr path as the active runtime and the OCI docs as historical evidence
plus restoration reference.

## Current Active Recovery Runtime

Validated on 2026-06-29 IST:

| Area | Verified state |
|---|---|
| Local runtime | Docker Desktop Compose stack from `docker-compose.live.single.yml` |
| Backend | `phoenix-v9-backend`, healthy, `APP_ENV=production`, `TRADE_MODE=LIVE`, `REQUIRE_LIVE_TRADE_MODE=true` |
| Web | `phoenix-v9-web`, healthy on `127.0.0.1:80` |
| Database | Windows PostgreSQL 18 database `phoenix`, user `phoenix_app`; broker secrets are stored in Postgres |
| Public endpoint | `https://app.phoenixtechnosolutions.in` |
| Vultr proxy | `phoenix-proxy` at `65.20.69.50`, nginx HTTPS, Let's Encrypt certificate valid until 2026-09-26 18:16:46 UTC |
| Tunnel owner | Docker sidecar `phoenix-v9-vultr-tunnel`, service `vultr-tunnel`, reverse SSH to Vultr `127.0.0.1:18080` |
| Fallback tunnel | Windows Scheduled Task `Phoenix Vultr Reverse Tunnel` remains installed but disabled |
| Strategy config | EMA20-only for the local LIVE-capable account configuration |
| OI/ML sidecar | Dormant; retained historical assets are not live order authority |
| Public health | `https://app.phoenixtechnosolutions.in/readyz` and `/health` return HTTP 200 |

Do not expose Postgres to the internet. Do not use `http://65.20.69.50` for
login or live operations; use the HTTPS domain only.

## Last Verified OCI VM State

Last verified against the VM: 2026-06-20 12:48 UTC.
Phoenix DB backup cron verified on the VM: 2026-06-23 14:21 UTC
with a schema-only dump and restore-list dry run.
EMA20 was enabled for the intended LIVE account and the OI/ML research sidecar
was persistently dormant with its historical database, image, and logs retained.

| Area | Verified state |
|---|---|
| Host | `phoenix-vm` |
| Repo checkout | `/opt/phoenix/app` |
| Git state on VM | branch `main`; verify checkout SHA and running image tags from release evidence for each rollout |
| Compose project | `phoenix-oci-live` |
| Compose files in use | `/opt/phoenix/app/docker-compose.oci-live.yml`, `/opt/phoenix/phoenix-override.yml` |
| Env file in use | `/opt/phoenix/phoenix-deploy.env` |
| Backend container | `phoenix-oci-backend`, local `phoenix-local-backend:local-<git-sha>` image; cron stops it outside scheduled runtime |
| Web container | `phoenix-oci-web`, local `phoenix-local-nginx:local-<git-sha>` image, healthy |
| Database | VM-local `phoenix-oci-postgres` container, `postgres:16-alpine`, Compose-managed and Docker-healthy |
| Database backup | Root cron `/etc/cron.d/phoenix-postgres-backup` runs `/opt/phoenix/scripts/backup-postgres.sh` at 23:30 IST Monday-Friday; dumps are stored under `/opt/phoenix/backups/postgres` and verified with `pg_restore -l` |
| Watchdog | `phoenix-oci-watchdog`, `docker:cli`; observe-only, no Docker socket or mounts |
| OI/ML shadow sidecar | No container is present; retained image `phoenix-oi-ml-shadow:oi-ml-shadow-e5e13bd` and operator Compose remain with restart policy `no`, runner/snapshotter/backend monitoring disabled, and historical Postgres/image/log evidence preserved |
| Backend command | `python -m app.main` |
| Public backend exposure | backend port `8080` is container-only; nginx exposes host ports `80` and `8443` |
| Health checks | backend container: `/health`, `/ready`, `/readyz`, `/health/summary`, `/health/alerts`, `/health/mitigations`; nginx/host: `/health`, redacted `/readyz`, redacted `/health/summary`, JSON `/health/alerts`, JSON `/health/mitigations` |
| Frontend health rendering | Overview/Safety use authenticated `/admin/health/summary` for internal schema, watchdog, and account-count fields, then fall back to redacted public `/health/summary` |
| Runtime mode evidence | `APP_ENV=production`, `TRADE_MODE=LIVE`, `REQUIRE_LIVE_TRADE_MODE=true`; `/health` reports `order_path=strategy_bridge_order_router`; `/health/summary` reports `operating_mode=HUB_AUTHORITATIVE` |
| Readiness evidence | backend-local `/readyz` returned HTTP 200; public `/readyz` returned only redacted readiness and universe-health fields |
| Secrets | secret files under `/run/secrets`; deployed permission validator passes; values must never be copied into git |

## 2026-06-20 Runtime Changes

- EMA20-only LIVE routing was enabled for the intended Angel account. Broker
  login, strict startup recovery, fresh position/order sync, flat broker and
  ownership state, inactive kill switch, and `/readyz=200` were verified.
- The LIVE leader lease is `phoenix-oci-live`; non-EMA strategy configs remain
  disabled and `AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING=1`.
- The canonical Phoenix hostname is forwarded to the backend Host guard. Login
  now reaches normal request validation while malformed/unapproved hosts remain
  rejected.
- The OI/ML sidecar was made dormant. No data was deleted; reactivation requires
  an explicit reviewed change to both runner/snapshotter enablement and backend
  monitoring.

Current drift that operators must not normalize:

- The VM is not running OCIR images; Phoenix backend and nginx run local
  images tagged `local-<git-sha>`.
- The VM is not using an external OCI Database for PostgreSQL; it is using a VM-local Postgres container.
- The backend has source-file bind mounts from `/opt/phoenix/app` into the container.
- `CONTROL_PLANE_PG_SSLMODE=prefer` and `LIVE_PG_SSL_SKIP_CHECK=true` are present because the DB is local to the VM.
- Phoenix still shares the VM with capped unrelated public workloads; dedicated
  Phoenix hosting remains the preferred future capacity state.
- Previously exposed secret values still require rotation even though file permissions are now hardened.

See [OCI VM Runtime Evidence](docs/OCI_VM_RUNTIME.md) for the evidence table.

## Operator Reading Order

1. [Docker Desktop LIVE Deployment](docs/runbooks/docker_desktop_live_deployment.md)
2. [Vultr Reverse Proxy For Local Phoenix](docs/runbooks/vultr_reverse_proxy.md)
3. [Phoenix Encyclopedia](docs/ENCYCLOPEDIA.md)
4. [Architecture Contract](ARCHITECTURE.md)
5. [Documentation Audit](docs/DOCUMENTATION_AUDIT.md)
6. [OCI VM Runtime Evidence](docs/OCI_VM_RUNTIME.md)
7. [Kill Switch](docs/runbooks/kill_switch.md)
8. [Strategy Runtime Diagnostics](docs/runbooks/strategy_runtime_diagnostics.md)
9. [Broker Credential Update](docs/runbooks/update_broker_credentials.md)
10. [Restore Drill](docs/runbooks/restore_drill.md)
11. [OCI LIVE Deployment Runbook](docs/runbooks/oci_live_deployment.md)
12. [Release Evidence](docs/runbooks/release_evidence.md)
13. [OCI Runtime Hardening](docs/runbooks/oci_runtime_hardening.md)
14. [Phoenix Postgres Backup](docs/runbooks/postgres_backup.md)
15. [OI/ML Shadow Sidecar](docs/runbooks/oi_ml_shadow_sidecar.md)
16. [OI/ML Data Source Approval](docs/runbooks/oi_ml_data_source_approval.md)
17. [OI/ML CE Seller Rollout](docs/runbooks/oi_ml_ce_seller_rollout.md)

Cloud Run, GCP Secret Manager, Firestore, BigQuery authority, and OCIR-only
deployment material remain roadmap/reference unless a future audit proves they
are active. Docker Desktop is current only for the documented local recovery
deployment while OCI is unavailable.

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
sudo cat /etc/cron.d/phoenix-postgres-backup
sudo tail -n 80 /opt/phoenix/logs/phoenix-postgres-backup.log
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
docker/vultr-tunnel/              Reverse SSH tunnel sidecar image
migrations/                       SQL migrations and bootstrap assets
scripts/                          Operator and release utility scripts
ops/cron/                         Root cron definitions for VM-installed jobs
tests/                            Pytest suite
docs/OCI_VM_RUNTIME.md            Current VM evidence snapshot
docs/ENCYCLOPEDIA.md              Current runtime glossary and endpoint behavior
docs/runbooks/docker_desktop_live_deployment.md Active local Docker Desktop runbook
docs/runbooks/vultr_reverse_proxy.md Active Vultr proxy/tunnel runbook
docs/runbooks/oci_live_deployment.md Current OCI VM operator runbook
docs/runbooks/postgres_backup.md  VM-local Postgres backup automation runbook
docs/runbooks/oi_ml_shadow_sidecar.md OI/ML shadow sidecar progress and gates
docs/runbooks/oi_ml_data_source_approval.md OI/ML option-chain data source approval gate
docs/runbooks/oi_ml_ce_seller_rollout.md OI/ML promotion and rollback gates
docker-compose.oci-live.yml       Base Compose file used with the VM override
docker-compose.live.single.yml    Active local Docker Desktop recovery Compose file
phoenix-override.yml.example      Template mirroring the current VM override shape
```

## Secret Rule

Never commit or paste real values for broker credentials, database passwords,
admin keys, JWT/session/HMAC secrets, TOTP/PIN values, tokens, private IPs, or
OCI identifiers. Documentation may list required variable names only.

## OI/ML Shadow Promotion Guard

OI/ML shadow decisions are not promotable when produced by
`OI_ML_SHADOW_SCORER=constant`. Promotable shadow evidence requires trained
LightGBM artifacts, a passed model-validation report, fresh FULL quotes with IV
and Greeks, latest validation status not `ERROR`, and virtual lifecycle rows
that reach `FLAT` with realized dry-run PnL by the cutoff. The retained sidecar
configuration keeps the scorer at `missing`, but the runner, snapshotter,
restart policy, and backend monitoring are disabled. It cannot ingest or
produce shadow entries until an explicit reviewed reactivation; the model and
10 clean-session proof are still required afterward.
