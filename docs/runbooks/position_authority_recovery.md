# Position Authority Recovery

Use this workflow when broker/current-position evidence proves the contract is
flat but Phoenix still reports a degraded or reconciling internal position
record.

Ownership cleanup is automatic after corroborated broker-flat evidence. As of
the 2026-05-25 deployment, broker-flat auto-recovery also runs after every
successful order-sync cycle, after external-fill reconciliation. As of the
2026-06-02 live hardening, it can also clear zero-quantity
`FLAT_PENDING_CONFIRMATION` records after broker-flat confirmation. It can clear
a stale zero-quantity `FLAT_PENDING_CONFIRMATION`, `RECONCILING`, `DEGRADED`, or
`RECOVERY_PENDING` lifecycle record only when all of these are true:

- broker positions snapshot is fresh and successful
- broker orders snapshot is fresh and not older than the positions snapshot
- Phoenix internal net quantity for the record is zero
- broker position evidence for the contract is flat
- no active matching broker order exists

On success, auto-recovery persists the internal record as `FLAT`, removes the
matching ownership row, attempts degraded-scope recovery, and emits audit
evidence. If broker evidence shows a nonzero position, Phoenix must not auto
clear the record; position trailing lock may manage that external/manual-owned
broker position.

Manual recovery below is the fallback when auto-recovery does not converge.

1. Check the dashboard status contract:

   ```bash
   docker exec phoenix-oci-backend \
     curl -sS http://localhost:8080/dashboard/status
   ```

   Confirm `readiness.reason` is `position_authority_degraded` and note the
   `readiness.position_state_counts`, `readiness.degraded_scope_count`, and
   `readiness.degraded_scope_samples` values. The sample list includes the
   blocking scope key and recovery attempt count for the first few active
   degraded scopes.

2. Fetch recovery evidence:

   ```bash
   docker exec phoenix-oci-backend \
     curl -sS -H "X-Admin-Key: <operator-key>" \
     http://localhost:8080/admin/state/position-authority/recovery
   ```

   Review each `records[]` item. The safe path requires
   `broker_evidence.status = "flat"` and `recovery.allowed_without_force = true`.
   The payload intentionally excludes broker credentials and secrets.

3. Clear the stuck internal record only after broker-flat evidence is present:

   ```bash
   docker exec phoenix-oci-backend \
     curl -sS -X POST -H "X-Admin-Key: <operator-key>" \
     -H "Content-Type: application/json" \
     -d '{"scope_key":"<scope_key>","reason":"broker_flat_verified_recovery","force":false}' \
     http://localhost:8080/admin/state/clear-position-record
   ```

   The endpoint refuses nonzero broker evidence with HTTP 409. `force=true` is a
   break-glass override and is recorded in the audit event.
   When `force=false` succeeds with `broker_evidence.status = "flat"`, the
   endpoint also attempts to recover the matching in-memory degraded scope. A
   backend restart should not be required just to remove the recovered scope
   from readiness.

4. Recheck readiness from the same workflow:

   ```bash
   docker exec phoenix-oci-backend \
     curl -sS http://localhost:8080/dashboard/status
   docker exec phoenix-oci-backend \
     curl -sS http://localhost:8080/readyz
   docker exec phoenix-oci-backend \
     curl -sS http://localhost:8080/health/summary
   ```

   Readiness should recover once no degraded scopes or DEGRADED/RECONCILING
   position records remain. If it does not, inspect the clear response's
   `degraded_scope_recovered` / `degraded_scope_recovery_error` fields, fetch
   recovery evidence again, and review the remaining record or degraded scope.
   Public nginx `/health/summary` is redacted; use backend-local
   `/health/summary` or authenticated `/admin/health/summary` for internal
   recovery diagnostics.
