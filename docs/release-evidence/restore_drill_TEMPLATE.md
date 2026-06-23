# Postgres Restore Drill — Evidence Record

> **Instructions**: Copy this file to `restore_drill_YYYYMMDD.md`, fill in every
> field, and commit the completed file before approving LIVE go-live. An empty or
> template file does not satisfy the release gate.
>
> See `docs/runbooks/restore_drill.md` for step-by-step procedures.

---

## Drill metadata

| Field | Value |
|---|---|
| Drill date | YYYY-MM-DD |
| Operator | (name / handle) |
| Reviewer / witness | (name / handle) |
| Postgres host used | (e.g. `host.docker.internal:5432` or Cloud SQL instance) |
| Backup snapshot date | YYYY-MM-DD HH:MM UTC |
| Backup source path | (e.g. `/opt/phoenix/backups/postgres/phoenix_YYYYMMDDTHHMMSSZ.dump`) |
| Backup automation evidence | (cron/manual command and log timestamp) |
| Phoenix version / commit | (git SHA) |

---

## Pass / fail summary

| Check | Result | Notes |
|---|---|---|
| Backup snapshot located and accessible | PASS / FAIL | |
| Backup restore-list verification passed | PASS / FAIL | `pg_restore -l` evidence: |
| Restore completed without errors | PASS / FAIL | |
| Schema guard passed post-restore | PASS / FAIL | |
| `order_submission_outbox` rows present | PASS / FAIL | count: |
| `position_ownership_ledger` rows present | PASS / FAIL | count: |
| `internal_position_records` rows present | PASS / FAIL | count: |
| `trade_processed_markers` rows present | PASS / FAIL | count: |
| `kill_switch_state` rows present or empty | PASS / FAIL | count: |
| Phoenix started cleanly against restored DB | PASS / FAIL | |
| `/readyz` returned 200 after startup | PASS / FAIL | |
| Release evidence snapshot captured | PASS / FAIL | |

**Overall: PASS / FAIL**

---

## Measured recovery objectives

| Metric | Measured value | Target |
|---|---|---|
| RTO (time from failure detection to operational) | HH:MM | < 30 min |
| RPO (maximum data loss window) | HH:MM | < 1 trading session |

---

## Issues found during drill

<!-- List any steps that failed or required deviation from the runbook. -->

1. (none)

---

## Runbook corrections required

<!-- List any corrections to docs/runbooks/restore_drill.md identified during this drill. -->

1. (none)

---

## Sign-off

- Operator: _____________________ Date: ___________
- Reviewer: _____________________ Date: ___________
