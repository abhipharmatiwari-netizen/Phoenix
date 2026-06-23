# OCI Runtime Hardening

Purpose: reduce the runtime drift observed on the OCI VM without changing live
trading behavior during normal documentation or review work.

Status: the 2026-06-06 hardening pass has already applied the Postgres
compose-adoption, watchdog no-socket recreation, secret-permission validation,
and storage expansion steps on the current OCI VM. The Phoenix DB backup cron
was installed and dry-run verified on 2026-06-23. Keep this runbook for evidence
capture, repeatable maintenance, and rollback.

Scope: the current OCI VM deployment only. Do not apply these steps to the live
VM without an approved maintenance window, a fresh database backup, and operator
approval to restart containers.

## Current Drift Being Addressed

| Drift | Verified current state | Target direction |
|---|---|---|
| Postgres ownership | `phoenix-oci-postgres` is Compose-managed and Docker-healthy on the current VM | keep Postgres under the `vm-local-postgres` profile and retain health evidence |
| Image provenance | backend/web run `phoenix-local-*` images | use immutable image tags from the approved image build path |
| Source overlays | backend has selected source-file bind mounts | remove overlays after the image contains those exact files |
| Watchdog behavior | `phoenix-oci-watchdog` has no mounts on the current VM | keep the observe-only no-socket contract; treat Docker socket mounts as drift |

## Preconditions

- Live trading is stopped or the operator has approved the maintenance action.
- `/opt/phoenix/pgdata` has a verified backup. Use
  `docs/runbooks/postgres_backup.md` and confirm the log contains
  `backup complete` and `verified=true` before maintenance that can affect the
  database or its container.
- The current runtime evidence in `docs/OCI_VM_RUNTIME.md` has been refreshed.
- `/run/secrets/control_plane_pg_password` exists on the VM.
- `/opt/phoenix/phoenix-deploy.env` sets `CONTROL_PLANE_PG_SSLMODE_HOST=prefer`
  for VM-local Postgres validation; external/cloud Postgres should use
  `require`.
- `docker port phoenix-oci-postgres` returns no published host ports. If a
  Postgres port is published or the DB host changes away from a recognized
  local Docker host, remove `LIVE_PG_SSL_SKIP_CHECK=true` and require encrypted
  Postgres transport before LIVE startup.
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

- `phoenix-oci-postgres` exists, uses `/opt/phoenix/pgdata`, and reports
  Docker health status `healthy`.
- backend source overlays are still present.
- watchdog inspect reports no mounts. Logs should not show nginx stop/start
  actions after the no-socket recreation.

## Phase 2 - Compose-Managed Postgres Candidate

The repository now contains an opt-in `vm-local-postgres` Compose profile in
`docker-compose.oci-live.yml`. It is active on the current VM, but remains
profile-gated in the manifest so a default Compose operation does not create a
second local database.

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

Do not run `up postgres` while an unmanaged `phoenix-oci-postgres` container is
running or while the current Compose-managed Postgres is already healthy. The
container name is intentionally the same so an accidental second database cannot
start beside production.

Maintenance-window migration outline:

1. Capture release evidence.
2. Capture and verify a database backup with the installed VM backup script:
   ```bash
   sudo /opt/phoenix/scripts/backup-postgres.sh
   sudo tail -n 80 /opt/phoenix/logs/phoenix-postgres-backup.log
   sudo cat /opt/phoenix/backups/postgres/latest.json
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
8. Re-run backend-local `/readyz`, backend-local `/health/summary`,
   authenticated `/admin/health/summary`, public redacted `/readyz` and
   `/health/summary`, and `/admin/release-evidence`.

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
recorded in the deployment record. A local image tag such as `local-4ba598f` is
acceptable only as an explicitly approved temporary state; immutable registry
tags are the target operating model.

## Phase 4 - Watchdog Contract

The current hardened watchdog is observe-only. It should poll backend `/health`
and log fail/recovery counts without Docker socket access, mounted host paths,
or nginx stop/start actions.

If future evidence shows Docker socket access or nginx mutations, treat that as
runtime drift. Recreate the watchdog with
`scripts/ops/recreate_oci_watchdog.sh` during an approved maintenance window and
capture `docker inspect phoenix-oci-watchdog --format '{{json .Mounts}}'` as
post-change evidence.

## Phase 5 - Secret And Env File Hygiene

Run after secret rotation or before any deployment evidence capture:

```bash
sudo PHOENIX_ROOT=/opt/phoenix \
  /opt/phoenix/app/scripts/ops/harden_oci_file_permissions.sh
```

This sets owner-only permissions on `/run/secrets/*`, active
`/opt/phoenix/phoenix-deploy.env`, and `phoenix-deploy.env` backups, then runs:

```bash
sudo PHOENIX_ROOT=/opt/phoenix \
  /opt/phoenix/app/scripts/ops/check_env_secret_material.sh
sudo /opt/phoenix/app/scripts/validate-live-secret-perms.sh
```

Both checks report file names and key names only. Do not paste env values,
secret file contents, broker credentials, cloud keys, or screenshots containing
those values into GitHub issues or runbooks.

## Phase 6 - Database Backup, Storage And Cleanup

The VM-local Phoenix Postgres backup cron is installed as:

```text
/etc/cron.d/phoenix-postgres-backup
0 18 * * 1-5 root /opt/phoenix/scripts/backup-postgres.sh
```

This is 23:30 IST Monday-Friday and intentionally skips Saturday/Sunday. The
script stores verified custom-format dumps in
`/opt/phoenix/backups/postgres`, writes evidence to
`/opt/phoenix/backups/postgres/latest.json`, logs to
`/opt/phoenix/logs/phoenix-postgres-backup.log`, uses `flock`, keeps local dumps
for 14 days by default, and requires 10 GB free space by default.

After deploying a repo change that updates `scripts/ops/backup_oci_postgres.sh`
or `ops/cron/phoenix-postgres-backup`, sync the installed VM paths and run the
non-data dry run:

```bash
sudo install -o root -g root -m 0755 \
  /opt/phoenix/app/scripts/ops/backup_oci_postgres.sh \
  /opt/phoenix/scripts/backup-postgres.sh

sudo install -o root -g root -m 0644 \
  /opt/phoenix/app/ops/cron/phoenix-postgres-backup \
  /etc/cron.d/phoenix-postgres-backup

sudo bash -n /opt/phoenix/scripts/backup-postgres.sh
sudo PHOENIX_PG_BACKUP_DRY_RUN=true /opt/phoenix/scripts/backup-postgres.sh
sudo tail -n 80 /opt/phoenix/logs/phoenix-postgres-backup.log
```

The dry run must include schema-only `pg_dump` and `pg_restore -l`
verification. It must not create a full `.dump` file. Run a full backup before
database-affecting maintenance:

```bash
sudo /opt/phoenix/scripts/backup-postgres.sh
sudo cat /opt/phoenix/backups/postgres/latest.json
```

Capture storage evidence before cleanup:

```bash
/opt/phoenix/app/scripts/ops/oci_storage_report.sh
```

The weekly cleanup script is allowed to prune stopped containers, dangling
images, build cache, old date-stamped log directories, and older local Phoenix
rollback images. It must preserve images currently used by running containers,
keep the latest configured rollback set, and never prune Docker volumes,
backups, `/run/secrets`, or database files.
The rollback image scope includes `phoenix-local-backend`,
`phoenix-local-nginx`, `phoenix-oi-ml-shadow`, and `aurelium` tags.

On the OCI VM, cron runs `/opt/phoenix/scripts/weekly-cleanup.sh`. After
deploying a repo change that updates `scripts/ops/weekly-cleanup.sh`, sync the
cron path and confirm the hashes match before relying on dry-run output:

```bash
sudo install -m 0755 \
  /opt/phoenix/app/scripts/ops/weekly-cleanup.sh \
  /opt/phoenix/scripts/weekly-cleanup.sh
sha256sum \
  /opt/phoenix/app/scripts/ops/weekly-cleanup.sh \
  /opt/phoenix/scripts/weekly-cleanup.sh
```

Preview the cleanup command first:

```bash
PHOENIX_CLEANUP_DRY_RUN=true \
  PHOENIX_CLEANUP_KEEP_LIVE_TAGS=3 \
  /opt/phoenix/scripts/weekly-cleanup.sh
```

The dry-run log must include `dry-run:` for destructive Docker prune commands.
If it does not, stop and resync the cron script before running cleanup.

Run the cleanup only after backups and active image tags have been recorded:

```bash
PHOENIX_CLEANUP_KEEP_LIVE_TAGS=3 \
  /opt/phoenix/scripts/weekly-cleanup.sh
```

The OCI LIVE Compose manifest enables the `disk_headroom_low` alert in
`/health/alerts` with these default thresholds:

- `ALERT_DISK_HEADROOM_PATH=/app/logs`
- `ALERT_DISK_MIN_FREE_GB=10`
- `ALERT_DISK_MAX_USED_PERCENT=90`

Capture `df -h /`, `docker system df`, and `/health/alerts` output after any
volume expansion, image build, or cleanup run. The disk alert must remain wired
before closing storage-headroom incidents; malformed alert thresholds fail
closed as a critical storage alert.

Classify Docker journal warnings as part of the cleanup review. BuildKit
attestation export warnings, transient Docker socket preface disconnects during
deploys, and one-off health-check timeouts are review notes when container
health and release evidence are green. Repeated image-signature validation
warnings, security-option deprecation warnings, or health-check timeouts outside
deploy windows should stay on the production backlog until the root cause or
explicit acceptance is documented.

## Phase 7 - Host Isolation Review

Phoenix LIVE should run on a dedicated VM. If another public workload remains
on the same host, record explicit risk acceptance and verify all of the
following before market operation:

- no unrelated container publishes public host ports that bypass the intended
  LB/security-list path;
- unrelated containers have CPU, memory, restart, log-retention, and storage
  limits;
- Docker storage, root filesystem headroom, and journal noise are reviewed after
  the co-tenant workload is running;
- the migration plan to move Phoenix or the co-tenant workload is tracked in
  the production backlog.

When the Aurelium co-tenant remains on the Phoenix host, deploy the resource
cap enforcement script and a root cron entry so container restarts cannot leave
the workload uncapped:

```bash
sudo install -m 0755 \
  /opt/phoenix/app/scripts/ops/enforce_cotenant_resource_caps.sh \
  /opt/phoenix/scripts/enforce-cotenant-resource-caps.sh

sudo tee /etc/cron.d/phoenix-cotenant-resource-caps >/dev/null <<'EOF'
*/5 * * * * root /opt/phoenix/scripts/enforce-cotenant-resource-caps.sh >> /opt/phoenix/logs/cotenant-resource-caps.log 2>&1
EOF
sudo chmod 0644 /etc/cron.d/phoenix-cotenant-resource-caps

sudo /opt/phoenix/scripts/enforce-cotenant-resource-caps.sh
```

The script uses `docker update` only. It must not stop, start, remove, or
recreate co-tenant containers. Verify active caps without printing env values:

```bash
docker inspect \
  aurelium-api-1 aurelium-clickhouse-1 aurelium-postgres-1 \
  --format '{{.Name}} cpus={{.HostConfig.NanoCpus}} memory={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}}'
```

Do not close the host-isolation finding from repository changes alone. It
requires VM evidence showing a dedicated host or documented compensating
controls, including current public-port review and resource-cap evidence.
