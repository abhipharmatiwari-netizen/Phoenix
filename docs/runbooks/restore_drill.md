# Restore Drill Runbook

**Architecture reference:** restore, recovery, and failure-drill policy in `ARCHITECTURE.md`

This runbook defines how to test Phoenix backup and restore readiness. The goal is to prove that Phoenix can restart from durable state, reconcile correctly, and re-establish the automated LIVE runtime without guessing or silently losing authoritative state.

---

## Recovery targets

| Metric | Target | Meaning |
|---|---|---|
| **RTO** | 15 minutes | Time to restore service |
| **RPO** | 5 minutes | Maximum acceptable data loss window |

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
SELECT COUNT(*) FROM circuit_breaker_state;
SELECT COUNT(*) FROM sweep_states;
SELECT COUNT(*) FROM broker_credentials;
SELECT COUNT(*) FROM users;
```

Also verify that obviously stale stuck submissions are not silently ignored:

```sql
SELECT COUNT(*)
FROM order_submission_outbox
WHERE status = 'SUBMITTING'
  AND created_at < NOW() - INTERVAL '1 hour';
```

### Step 4 — Start Phoenix against the restored database

Use the bundled LIVE manifest and the same runtime secret process you use in production.

```powershell
docker compose -f .\docker-compose.live.single.yml up -d --build --force-recreate
```

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
