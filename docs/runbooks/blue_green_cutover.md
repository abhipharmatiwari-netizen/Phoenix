# Blue/Green Cutover Playbook

> **Status:** ROADMAP_ONLY / NOT CURRENTLY IMPLEMENTED ON THE OCI VM.
>
> The verified OCI VM has one Phoenix Compose project and one VM-local Postgres
> container. No blue/green deployment was observed. Use this document only as a
> future design reference until a fresh VM audit proves blue/green infrastructure
> exists.

**Architecture reference:** deployment and cutover policy in `ARCHITECTURE.md`

## Purpose

This playbook covers controlled blue/green cutover for the current hub-authoritative LIVE path.

Only one active writer may control a given live scope at a time.

---

## Scope

This playbook assumes:

- both environments use the same current LIVE contract
- Postgres is the shared authoritative operational store
- broker credentials are already present in Postgres for the active broker accounts when using the bundled broker-secret path
- the incoming environment will start, restore durable state, reconcile, and prove market-data/strategy readiness before taking live write authority for automated LIVE

This playbook does **not** approve legacy-authoritative cutover.

It also does not approve Cloud Run go-live, Firestore authority, or simultaneous active writers. For LIVE, cutover is allowed only when both environments can produce release evidence for the current runtime contract.

---

## Prerequisites

Before cutover, all of the following must be true:

- both blue and green builds were produced from approved release inputs
- the target Postgres database has passed the explicit migration step for this release
- both environments point to the same authoritative Postgres control plane
- green has been started at least once in a controlled pre-cutover validation run, or its release evidence proves the full LIVE tuple
- monitoring and log access are available for both environments
- operators know which environment currently owns live write authority

The bundled Compose LIVE path now treats migration execution as a release gate:

- `migrator` applies every repo-tracked `migrations/*.sql` file before `db-preflight`
- `db-preflight` verifies the exact tracked migration file set and checksums in `public.schema_migrations`
- cutover is blocked if that migration verification did not run successfully against the target database

---

## Pre-cutover checklist

- [ ] Confirm green resolves `TRADE_MODE=LIVE` inside the backend container.
- [ ] Confirm green resolves hub-authoritative mode inside the backend container.
- [ ] Confirm the green target database passed the explicit migration gate for this release.
- [ ] Confirm green resolves `DISABLE_STREAM_WORKER=false`, or the release explicitly documents an approved replacement market-data plane.
- [ ] Confirm green startup validation passes.
- [ ] Confirm green can reach Postgres.
- [ ] Confirm green can reach required broker endpoints.
- [ ] Confirm green market-data / strategy startup is healthy for automated LIVE.
- [ ] Confirm there are no stuck non-terminal submissions that require manual review.
- [ ] Confirm kill-switch state is persisted.
- [ ] Confirm ownership and lifecycle state are present in Postgres.
- [ ] Confirm the operator knows whether cutover should happen flat or with positions carried across reconciliation.

---

## Phase 1 — Drain the blue writer

1. **Freeze new entries on blue.**
   Use the approved operational control for entry blocking in your environment.

2. **Wait for in-flight submissions to settle.**

   Example query:

   ```sql
   SELECT COUNT(*)
   FROM order_submission_outbox
   WHERE status NOT IN ('FILLED', 'REJECTED', 'CANCELLED', 'EXPIRED', 'FAILED');
   ```

3. **If required by the release plan, flatten before cutover.**
   Use the approved live operational path for EOD/manual exposure reduction.

4. **Stop blue gracefully.**

   Example with the bundled Docker manifest:

   ```powershell
   docker compose -p phoenix-blue -f .\docker-compose.live.single.yml down --remove-orphans
   ```

5. **Verify blue no longer owns the writer role.**
   Ownership handoff must be observable through logs, health signals, and the absence of an active writer.

---

## Phase 2 — Start the green writer

1. **Start green.**

   ```powershell
   docker compose -p phoenix-green -f .\docker-compose.live.single.yml up -d --build --force-recreate
   ```

   Do not proceed if `migrator` did not exit successfully. In the bundled LIVE path, green is not eligible for cutover unless:

   - `migrator` applied or verified every repo migration file against the target Postgres database
   - `db-preflight` then verified the tracked migration set from `public.schema_migrations`

2. **Verify startup restoration and reconciliation.**
   Green must restore durable state before it begins live write activity.

3. **Watch for these signals in green logs:**

   - migration apply/verify succeeded for the exact repo migration set
   - startup validation succeeded
   - operating mode resolved to hub-authoritative
   - recovery pending markers restored
   - reconciliation completed
   - stream-worker market-data / strategy plane started successfully for automated LIVE

4. **Do not treat green as the active writer until reconciliation is complete.**

5. **Do not treat green as automated-LIVE ready until fresh market-data / strategy signals are healthy.**

---

## Phase 3 — Post-cutover validation

- verify the UI is reachable through nginx
- verify health endpoints are healthy
- verify position state agrees with broker evidence after reconciliation
- verify kill-switch state is correct
- verify ownership records remain intact
- verify no unexpected replay loop is occurring
- verify live marks, bars, and strategy runtime are fresh for automated LIVE

---

## Rollback procedure

If green is unhealthy after cutover:

1. stop green gracefully
2. confirm green released active write ownership
3. start blue again with the same LIVE manifest
4. allow blue to restore durable state, reconcile, and re-establish market-data/strategy health before resuming automated live write activity
5. repeat the validation checks before considering blue active again

---

## State that survives cutover

The following state is expected to survive blue/green transitions because it is stored in authoritative Postgres-backed stores:

| State | Authoritative store | Expected to survive cutover |
|---|---|---|
| submission outbox | Postgres | Yes |
| lifecycle state and durable markers | Postgres | Yes |
| ownership ledger | Postgres | Yes |
| kill-switch / circuit-breaker state | Postgres | Yes |
| sweep / EOD state | Postgres | Yes |
| control-plane configuration | Postgres | Yes |
| broker credentials | Postgres | Yes |

Derived reporting outputs such as CSV files or analytics exports are not authoritative cutover gates.

---

## Required release evidence

For any real cutover, keep all of the following:

- pre-cutover `docker compose ps` for blue and green
- green rendered compose manifest
- `migrator` logs showing migration apply success for the target database
- query output or log evidence proving `public.schema_migrations` matches the exact repo migration set for the release
- green effective backend LIVE env output
- health output before and after cutover
- log excerpts showing startup validation and reconciliation success
- log excerpts showing stream-worker or approved replacement market-data plane health
- operator notes describing whether cutover happened flat or with open positions carried through reconciliation
