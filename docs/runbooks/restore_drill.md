# Restore Drill Runbook

> **Current runtime note:** as of 2026-06-29, active production Postgres is the
> Windows PostgreSQL 18 `phoenix` database used by the Docker Desktop/Vultr
> stack. The `phoenix-oci-postgres` backup material below is historical
> restoration evidence only unless a future migration issue reinstates OCI.

**Architecture reference:** restore, recovery, and failure-drill policy in `ARCHITECTURE.md`

## Purpose

This runbook defines how to test Phoenix backup and restore readiness. The goal is to prove that Phoenix can restart from durable state, reconcile correctly, and re-establish the automated LIVE runtime without guessing or silently losing authoritative state.

## Scope

Use this for controlled restore drills and recovery-environment validation. It does not approve restoring directly over an active LIVE database, Cloud Run go-live, Firestore authority, or CSV/local-file recovery as authoritative state.

## Preconditions

- You have a backup or PITR target for the authoritative Postgres database.
- You can stop Phoenix before restoring into the target database.
- You have the same runtime secret process used by the deployment path.
- You can capture SQL counts, container logs, `/readyz`, and release evidence after startup.

---

## Recovery targets

| Metric | Target | Notes |
|---|---|---|
| **RTO** | < 30 minutes | Demonstrated 18 min on local Docker Desktop (2026-04-25 drill). Shorter targets require PITR-capable infrastructure (Cloud SQL). |
| **RPO** | < 1 trading session | Local Docker Desktop is bounded by scheduled `pg_dump`; the retired OCI VM cron evidence is historical. Cloud SQL with WAL/PITR is roadmap/reference only unless a future audit proves it is active. |

---

## What must survive restore

At minimum, the drill must prove recovery of:

- control-plane configuration
- broker account mappings
- broker credentials
- submission outbox
- lifecycle state and durable markers
- ownership ledger
- circuit-breaker / kill-switch state
- sweep and EOD state
- enough market-data / strategy configuration to re-establish the automated LIVE runtime after startup

CSV files and other convenience outputs are not authoritative recovery targets.

---

## Backup procedure

### Automated backups

Use your database platform's production backup mechanism. At minimum, maintain:

- regular full backups
- WAL / point-in-time recovery where supported
- tested retention policy
- operator access to restore into an isolated target database

**RPO by deployment platform:**
- **Historical OCI VM** (`phoenix-oci-postgres`): backup cadence was a VM-local
  custom-format `pg_dump` at 23:30 IST Monday-Friday. WAL archiving / PITR is
  not currently configured by this repo path. RPO is bounded by the latest
  verified weekday dump; run a manual full backup before weekend maintenance or
  any database-affecting change.
- **Local Docker Desktop** (host-local Postgres): backup cadence is limited to scheduled `pg_dump`. WAL archiving / point-in-time recovery (PITR) is not available without additional configuration. RPO is bounded by the `pg_dump` schedule (e.g. daily = up to one trading session of data loss).
- **Cloud Run + Cloud SQL**: roadmap/reference only in this repo. Cloud SQL can provide WAL-based PITR, but Cloud Run is not the current approved go-live path.

### Historical OCI VM backup commands

Verify the installed backup job:

```bash
sudo cat /etc/cron.d/phoenix-postgres-backup
sudo tail -n 80 /opt/phoenix/logs/phoenix-postgres-backup.log
sudo cat /opt/phoenix/backups/postgres/latest.json 2>/dev/null || true
```

Run a non-data dry run, which performs schema-only dump and restore-list
verification:

```bash
sudo PHOENIX_PG_BACKUP_DRY_RUN=true /opt/phoenix/scripts/backup-postgres.sh
```

Run a full VM-local backup before a restore drill or maintenance action:

```bash
sudo /opt/phoenix/scripts/backup-postgres.sh
sudo cat /opt/phoenix/backups/postgres/latest.json
```

Use the resulting `/opt/phoenix/backups/postgres/phoenix_<UTC>.dump` file as
the restore source for an isolated target. Do not restore over active LIVE
Postgres.

### Manual backup example

```bash
pg_dump -Fc -f phoenix_backup_$(date +%Y%m%d_%H%M%S).dump $DATABASE_URL
pg_dump -Fc --schema-only -f phoenix_schema_$(date +%Y%m%d).dump $DATABASE_URL
```

---

## Restore procedure

### Step 1 — Stop Phoenix

Use the same manifest that is bundled for LIVE:

```powershell
docker compose -f .\docker-compose.live.single.yml down --remove-orphans
```

### Step 2 — Restore the database

Example using `pg_restore`:

```bash
pg_restore -d phoenix_restored -Fc phoenix_backup.dump
```

If you use base backup plus WAL replay, follow your PostgreSQL platform procedure for that environment.

### Step 3 — Verify authoritative tables exist and contain data

At minimum, verify the restored database contains the expected operational stores.

Example checks:

```sql
SELECT COUNT(*) FROM order_submission_outbox;
SELECT COUNT(*) FROM position_ownership_ledger;
SELECT COUNT(*) FROM internal_position_records;
SELECT COUNT(*) FROM circuit_breaker_state;
SELECT COUNT(*) FROM sweep_states;
SELECT COUNT(*) FROM broker_credentials;
SELECT COUNT(*) FROM users;
```

Confirm that the `internal_position_records` row count matches the number of open positions expected from the backup window. Non-terminal rows in that table (position_state not in `FLAT`, `NONE`) will enter reconciliation on startup — verify these are expected, not artifacts of a stale backup.

Also verify that obviously stale stuck submissions are not silently ignored:

```sql
SELECT COUNT(*)
FROM order_submission_outbox
WHERE status = 'SUBMITTING'
  AND created_at < NOW() - INTERVAL '1 hour';
```

### Step 4 — Start Phoenix against the restored database

Use the active Docker Desktop/Vultr deployment path and the same runtime secret
process you use in production. The OCI example below is retained only for
approved restoration drills:

```bash
cd /opt/phoenix/app
CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
docker compose \
  -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-deps backend nginx
```

The Docker Desktop command is retained only in the Docker Desktop runbook and is
not current production guidance.

### Step 5 — Validate startup, reconciliation, and market-data readiness

Check all of the following:

- startup validation passes
- backend resolves the required LIVE tuple
- ownership is restored
- kill-switch state is restored
- reconciliation completes
- stream-worker market-data / strategy plane restarts successfully for automated LIVE, or the approved replacement plane does
- Docker liveness endpoints are healthy
- backend-local `/readyz` returns 200 before automated LIVE entries resume

Example checks:

```bash
docker logs --tail 200 phoenix-oci-backend
docker exec phoenix-oci-backend curl -sS http://localhost:8080/readyz
docker exec phoenix-oci-backend curl -sS http://localhost:8080/health/summary
curl -sS http://localhost/readyz
curl -sS http://localhost/health/summary
curl -sS http://localhost/health/alerts
curl -sS http://localhost/health/mitigations
```

The public nginx `/readyz` and `/health/summary` responses are redacted. For
post-restore schema, watchdog, and tracked-account details, use backend-local
`/health/summary` or authenticated `/admin/health/summary`.

---

## Drill schedule

| Drill | Frequency | Environment |
|---|---|---|
| Full restore into isolated target | Monthly | Staging / recovery environment |
| PITR / WAL replay validation | Weekly | Recovery environment |
| Schema migration rollback rehearsal | Per release | Staging |

---

## Evidence to keep after each drill

Record all of the following:

1. date and operator
2. backup source and age
3. backup cron or manual backup command used
4. `pg_restore -l` verification status
5. actual RTO achieved
6. actual RPO achieved
7. tables verified
8. startup / reconciliation outcome
9. market-data / strategy plane outcome for automated LIVE
10. pass / fail decision
11. corrective actions, if any

A restore drill is complete only when Phoenix has restarted on the restored data, re-established the required LIVE runtime, and passed the post-restore validation checks.

## Failure handling and rollback

If restore validation fails, keep the restored stack stopped or isolated. Do not point LIVE traffic at it. Restore the last known-good database target or keep the current production stack active, capture the failing SQL/log evidence, and open an incident follow-up before retrying.

---

## Evidence file

After each drill, copy `docs/release-evidence/restore_drill_TEMPLATE.md` to
`docs/release-evidence/restore_drill_YYYYMMDD.md`, fill in all fields, and commit the
file. The completed evidence file is a required pass criterion for LIVE deployment approval
(see `docs/release-evidence/README.md`).
