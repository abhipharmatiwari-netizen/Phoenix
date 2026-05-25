# OCI Runtime Hardening

Purpose: reduce the runtime drift observed on the OCI VM without changing live
trading behavior during normal documentation or review work.

Scope: the current OCI VM deployment only. Do not apply these steps to the live
VM without an approved maintenance window, a fresh database backup, and operator
approval to restart containers.

## Current Drift Being Addressed

| Drift | Verified current state | Target direction |
|---|---|---|
| Postgres ownership | `phoenix-oci-postgres` runs outside the labelled Compose project and has no Docker healthcheck | move Postgres under an opt-in Compose profile with a healthcheck |
| Image provenance | backend/web run `phoenix-local-*` images | use immutable image tags from the approved image build path |
| Source overlays | backend has selected source-file bind mounts | remove overlays after the image contains those exact files |
| Watchdog behavior | `phoenix-oci-watchdog` actively stops/starts nginx | either formalize this as an approved recovery controller or remove the side effect |

## Preconditions

- Live trading is stopped or the operator has approved the maintenance action.
- `/opt/phoenix/pgdata` has a verified backup.
- The current runtime evidence in `docs/OCI_VM_RUNTIME.md` has been refreshed.
- `/run/secrets/control_plane_pg_password` exists on the VM.
- `/opt/phoenix/phoenix-deploy.env` sets `CONTROL_PLANE_PG_SSLMODE_HOST=prefer`
  for VM-local Postgres validation; external/cloud Postgres should use
  `require`.
- The operator has reviewed `docker compose config` output with secrets redacted.
- The current `phoenix-oci-postgres` environment has been checked. On the
  verified VM, `PGDATA=/var/lib/postgresql/data` and `PG_VERSION` exists
  directly in that directory. Do not start a candidate container with a
  different `PGDATA` path.

## Phase 1 - Read-Only Evidence

Run on the OCI VM:

```bash
cd /opt/phoenix/app

docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
docker inspect phoenix-oci-postgres --format '{{json .HostConfig.RestartPolicy}} {{json .Mounts}}'
docker inspect phoenix-oci-backend --format '{{json .Mounts}}'
docker logs --tail=120 phoenix-oci-watchdog
```

Expected evidence today:

- `phoenix-oci-postgres` exists and uses `/opt/phoenix/pgdata`.
- backend source overlays are still present.
- watchdog logs show whether nginx stop/start actions are still occurring.

## Phase 2 - Compose-Managed Postgres Candidate

The repository now contains an opt-in `vm-local-postgres` Compose profile in
`docker-compose.oci-live.yml`. It is not active by default.

Validate only:

```bash
cd /opt/phoenix/app

CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
docker compose \
  --profile vm-local-postgres \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  config
```

Do not run `up postgres` while the unmanaged `phoenix-oci-postgres` container is
running. The container name is intentionally the same so an accidental second
database cannot start beside production.

Maintenance-window migration outline:

1. Capture release evidence.
2. Capture and verify a database backup:
   ```bash
   sudo install -d -m 700 /opt/phoenix/backups
   BACKUP="/opt/phoenix/backups/phoenix_$(date -u +%Y%m%dT%H%M%SZ).dump"
   sudo sh -c "docker exec phoenix-oci-postgres pg_dump -U phoenix_app -d phoenix -Fc > '$BACKUP'"
   sudo sh -c "docker exec -i phoenix-oci-postgres pg_restore -l < '$BACKUP' >/dev/null"
   ```
3. Stop live trading services, including the watchdog, through the current
   Compose files:
   ```bash
   CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
   docker compose \
     -f docker-compose.oci-live.yml \
     -f /opt/phoenix/phoenix-override.yml \
     --env-file /opt/phoenix/phoenix-deploy.env \
     stop backend nginx backend-watchdog
   ```
4. Stop and rename the unmanaged Postgres container. Renaming preserves the old
   container metadata for rollback and frees the `phoenix-oci-postgres` name for
   Compose:
   ```bash
   OLD_PG="phoenix-oci-postgres-precompose-$(date -u +%Y%m%dT%H%M%SZ)"
   docker stop phoenix-oci-postgres
   docker rename phoenix-oci-postgres "$OLD_PG"
   echo "$OLD_PG" | sudo tee /opt/phoenix/postgres-precompose-container.txt
   ```
5. Start `postgres` through the `vm-local-postgres` profile:
   ```bash
   CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
   docker compose \
     --profile vm-local-postgres \
     -f docker-compose.oci-live.yml \
     -f /opt/phoenix/phoenix-override.yml \
     --env-file /opt/phoenix/phoenix-deploy.env \
     up -d postgres
   ```
6. Verify `docker ps` reports `phoenix-oci-postgres` as `healthy`, and verify
   the existing data directory was used:
   ```bash
   docker inspect phoenix-oci-postgres --format '{{json .State.Health.Status}}'
   docker exec phoenix-oci-postgres sh -lc 'printf "PGDATA=%s\n" "$PGDATA"; test -f "$PGDATA/PG_VERSION"'
   docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix -c "\dt"
   ```
7. Start backend and nginx through the OCI runbook.
8. Re-run `/readyz`, `/health/summary`, and `/admin/release-evidence`.

Rollback: stop and rename the Compose-managed `postgres` container, rename the
previous unmanaged container back to `phoenix-oci-postgres`, then start it:

```bash
OLD_PG="$(sudo cat /opt/phoenix/postgres-precompose-container.txt)"
FAILED_PG="phoenix-oci-postgres-compose-failed-$(date -u +%Y%m%dT%H%M%SZ)"

docker stop phoenix-oci-postgres || true
docker rename phoenix-oci-postgres "$FAILED_PG" || true
docker rename "$OLD_PG" phoenix-oci-postgres
docker start phoenix-oci-postgres
```

Do not restore a backup unless the data directory was modified or corrupted.

## Phase 3 - Immutable Image Candidate

Before removing source overlays, prove the image contains the same deployed files:

```bash
docker exec phoenix-oci-backend sh -lc 'sha256sum /app/app/server.py /app/app/runtime/app_runtime.py'
sha256sum /opt/phoenix/app/app/server.py /opt/phoenix/app/app/runtime/app_runtime.py
```

Only remove source-file bind mounts after the image digest and file checksums are
recorded in the deployment record. A local image tag such as `local-e7f1e29` is
acceptable only as an explicitly approved temporary state; immutable registry
tags are the target operating model.

## Phase 4 - Watchdog Decision

The current watchdog can stop and start nginx. Before changing it, decide which
behavior production wants:

| Option | Impact |
|---|---|
| formalize active recovery | keep Docker socket access, document nginx stop/start as expected, and alert on repeated recovery loops |
| observe-only | remove Docker socket access and nginx mutations; alert operators instead |

Changing watchdog behavior requires a separate deployment review because it can
change external availability during backend failure.
