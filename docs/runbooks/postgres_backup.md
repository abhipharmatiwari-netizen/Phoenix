# Phoenix Postgres Backup Runbook

Status: current for the OCI VM-local Postgres deployment. The backup cron was
installed and dry-run verified on 2026-06-23 14:21 UTC.

## Purpose

Automate VM-local Phoenix Postgres backups without exposing database passwords
or changing live trading state.

## Current Schedule

Phoenix uses a root-owned cron file:

```text
/etc/cron.d/phoenix-postgres-backup
```

Current cron entry:

```text
0 18 * * 1-5 root /opt/phoenix/scripts/backup-postgres.sh
```

This runs at 23:30 IST Monday-Friday. It does not run on Saturday or Sunday.
Cron is expressed in UTC because the VM cron environment is UTC.

## Runtime Paths

| Item | Path |
|---|---|
| Installed script | `/opt/phoenix/scripts/backup-postgres.sh` |
| Repo source script | `/opt/phoenix/app/scripts/ops/backup_oci_postgres.sh` |
| Repo cron source | `/opt/phoenix/app/ops/cron/phoenix-postgres-backup` |
| Backup directory | `/opt/phoenix/backups/postgres` |
| Backup files | `/opt/phoenix/backups/postgres/phoenix_<UTC>.dump` |
| Latest evidence | `/opt/phoenix/backups/postgres/latest.json` |
| Log file | `/opt/phoenix/logs/phoenix-postgres-backup.log` |
| Lock file | `/opt/phoenix/state/phoenix-postgres-backup.lock` |

## Backup Behavior

- Targets the VM-local `phoenix-oci-postgres` container.
- Dumps database `phoenix` as user `phoenix_app` using custom-format
  `pg_dump -Fc`.
- Writes a `.tmp` file first, verifies it with `pg_restore -l`, then moves it
  to the final `.dump` path.
- Writes `latest.json` only after a verified full dump.
- Uses `flock` so overlapping backups exit safely.
- Requires at least 10 GB free space at the backup path.
- Keeps local backups for 14 days by default.
- Does not read or print database password values.

## Limitations

This is a VM-local backup job. It does not provide WAL archiving, PITR, remote
replication, object-storage upload, or protection from total VM disk loss.
Off-host replication requires a separate approved job and evidence path.

The default retention and free-space guard can be changed only through explicit
environment variables:

```text
PHOENIX_PG_BACKUP_KEEP_DAYS
PHOENIX_PG_BACKUP_MIN_FREE_GB
```

## Verification

Check cron and daemon state:

```bash
sudo cat /etc/cron.d/phoenix-postgres-backup
sudo systemctl is-active crond
```

Run a dry-run verification. This performs a schema-only `pg_dump` and
`pg_restore -l` check without creating a full data backup:

```bash
sudo PHOENIX_PG_BACKUP_DRY_RUN=true /opt/phoenix/scripts/backup-postgres.sh
sudo tail -n 80 /opt/phoenix/logs/phoenix-postgres-backup.log
```

After a scheduled or manual full backup, verify evidence:

```bash
sudo cat /opt/phoenix/backups/postgres/latest.json
sudo ls -lh /opt/phoenix/backups/postgres/phoenix_*.dump
```

Expected log evidence for a full run includes `backup complete` and
`verified=true`.

## Manual Full Backup

Run a full backup before any maintenance action that can affect
`/opt/phoenix/pgdata`, the Postgres container, migrations, or restore testing:

```bash
sudo /opt/phoenix/scripts/backup-postgres.sh
```

Do not run destructive SQL or restore over the active LIVE database from this
runbook. Use the restore drill runbook for isolated restore validation.

## Updating The Installed Job

After a repo deploy that changes the backup script or cron source, sync the VM
paths and verify syntax:

```bash
sudo install -o root -g root -m 0755 \
  /opt/phoenix/app/scripts/ops/backup_oci_postgres.sh \
  /opt/phoenix/scripts/backup-postgres.sh

sudo install -o root -g root -m 0644 \
  /opt/phoenix/app/ops/cron/phoenix-postgres-backup \
  /etc/cron.d/phoenix-postgres-backup

sudo bash -n /opt/phoenix/scripts/backup-postgres.sh
sudo PHOENIX_PG_BACKUP_DRY_RUN=true /opt/phoenix/scripts/backup-postgres.sh
```

## Failure Handling

If a backup fails:

1. Do not delete existing `.dump` files.
2. Capture the latest backup log, cron file, `df -h /`, and
   `docker inspect phoenix-oci-postgres` health status.
3. Confirm `phoenix-oci-postgres` is running and healthy.
4. Check whether free space fell below the configured minimum.
5. Run the dry-run verification before attempting another full backup.

If the latest full backup is older than the last completed trading session,
treat backup freshness as a release/maintenance blocker unless the operator
explicitly accepts the risk.
