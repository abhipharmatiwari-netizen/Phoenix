# Restore Drill Runbook

> **Current OCI VM note:** verified production Postgres is the
> `phoenix-oci-postgres` container with data mounted from `/opt/phoenix/pgdata`.
> External/cloud database examples are non-current unless a fresh VM audit proves
> the deployment changed.

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
| **RPO** | < 1 trading session | Local Docker Desktop: bounded by `pg_dump` schedule. Cloud SQL with WAL/PITR: configurable to minutes. |

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
- **Local Docker Desktop** (host-local Postgres): backup cadence is limited to scheduled `pg_dump`. WAL archiving / point-in-time recovery (PITR) is not available without additional configuration. RPO is bounded by the `pg_dump` schedule (e.g. daily = up to one trading session of data loss).
- **Cloud Run + Cloud SQL**: roadmap/reference only in this repo. Cloud SQL can provide WAL-based PITR, but Cloud Run is not the current approved go-live path.

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

Use the bundled LIVE manifest for the active deployment path and the same runtime secret process you use in production.

```powershell
docker compose -f .\docker-compose.live.single.yml up -d --build --force-recreate
```

OCI Compose uses the equivalent `docker compose -f docker-compose.oci-live.yml -f /opt/phoenix/phoenix-override.yml --env-file /opt/phoenix/phoenix-deploy.env up -d --no-deps backend nginx` command after the database restore and secret refresh.

### Step 5 — Validate startup, reconciliation, and market-data readiness

Check all of the following:

- startup validation passes
- backend resolves the required LIVE tuple
- ownership is restored
- kill-switch state is restored
- reconciliation completes
- stream-worker market-data / strategy plane restarts successfully for automated LIVE, or the approved replacement plane does
- health endpoints are healthy

Example checks:

```powershell
docker compose -f .\docker-compose.live.single.yml logs --tail 200 backend
curl.exe http://localhost/health/summary
```

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
3. actual RTO achieved
4. actual RPO achieved
5. tables verified
6. startup / reconciliation outcome
7. market-data / strategy plane outcome for automated LIVE
8. pass / fail decision
9. corrective actions, if any

A restore drill is complete only when Phoenix has restarted on the restored data, re-established the required LIVE runtime, and passed the post-restore validation checks.

## Failure handling and rollback

If restore validation fails, keep the restored stack stopped or isolated. Do not point LIVE traffic at it. Restore the last known-good database target or keep the current production stack active, capture the failing SQL/log evidence, and open an incident follow-up before retrying.

---

## Evidence file

After each drill, copy `docs/release-evidence/restore_drill_TEMPLATE.md` to
`docs/release-evidence/restore_drill_YYYYMMDD.md`, fill in all fields, and commit the
file. The completed evidence file is a required pass criterion for LIVE deployment approval
(see `docs/release-evidence/README.md`).
