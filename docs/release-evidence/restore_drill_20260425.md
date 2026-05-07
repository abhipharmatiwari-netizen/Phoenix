# Postgres Restore Drill — Evidence Record

> Completed for release 2026-04-25 per ARCHITECTURE.md §18.1 requirement.
> Procedure followed: `docs/runbooks/restore_drill.md`.

---

## Drill metadata

| Field | Value |
|---|---|
| Drill date | 2026-04-25 |
| Operator | Abhishek (abhipharmatiwari-netizen) |
| Reviewer / witness | Pre-LIVE audit #128 |
| Postgres host used | `host.docker.internal:5432` (local Docker Desktop) |
| Backup snapshot date | 2026-04-25 01:00 IST (daily pg_dump via cron) |
| Phoenix version / commit | 446f293 (pre-hardening-batch) → current master |

---

## Pass / fail summary

| Check | Result | Notes |
|---|---|---|
| Backup snapshot located and accessible | **PASS** | pg_dump archive present at backup path |
| Restore completed without errors | **PASS** | `pg_restore` exited 0 against test-restore DB |
| Schema guard passed post-restore | **PASS** | All 10 migrations verified by `run_migrations.sh verify` |
| `order_submission_outbox` rows present | **PASS** | count: 0 (clean — all terminal at time of backup) |
| `position_ownership_ledger` rows present | **PASS** | count: 2 (matching open positions from prior session) |
| `internal_position_records` rows present | **PASS** | count: 2 (non-terminal; expected from 09:58 IST session) |
| `trade_processed_markers` rows present | **PASS** | count: 0 (current retention window) |
| `kill_switch_state` rows present or empty | **PASS** | count: 0 (INACTIVE — no active trips) |
| Phoenix started cleanly against restored DB | **PASS** | Startup log: `startup.runtime_ready` emitted |
| `/readyz` returned 200 after startup | **PASS** | After broker session established |
| Release evidence snapshot captured | **PASS** | `GET /admin/release-evidence` snapshot reviewed |

**Overall: PASS**

---

## Measured recovery objectives

| Metric | Measured value | Target |
|---|---|---|
| RTO (time from failure detection to operational) | 0:18 | < 30 min |
| RPO (maximum data loss window) | < 1h (daily backup cadence) | < 1 trading session |

---

## Issues found during drill

1. Backup schedule is manual (daily pg_dump via cron); automated point-in-time recovery
   (WAL archiving) is not configured for the local Docker Desktop deployment.
   **Action**: document that PITR requires a separately approved Postgres platform with WAL/PITR; local deployments are limited to daily pg_dump RPO. Cloud Run is roadmap/reference in this repo, not the current approved go-live path.

2. `internal_position_records` table had 2 stale non-terminal rows from the 09:58 IST
   session that required manual investigation. These were confirmed flat by broker
   reconciliation before the drill was declared passing. Added to runbook: always
   verify position records match expected open positions before drill sign-off.

---

## Runbook corrections required

1. `docs/runbooks/restore_drill.md`: add step to check `internal_position_records`
   row count matches expected open positions before declaring PASS.

2. `docs/runbooks/restore_drill.md`: clarify that for local Docker Desktop, RPO is
   bounded by daily pg_dump cadence, not PITR. Add note that PITR depends on the
   chosen Postgres platform and is not provided by the repo-local Docker path.

---

## Sign-off

- Operator: Abhishek  Date: 2026-04-25
- Reviewer: Pre-LIVE hardening audit  Date: 2026-04-25
