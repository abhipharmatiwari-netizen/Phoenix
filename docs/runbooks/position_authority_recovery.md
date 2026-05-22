# Position Authority Recovery

Use this workflow only when broker/current-position evidence proves the contract
is flat but Phoenix still reports a degraded or reconciling internal position
record.

Ownership cleanup is automatic after corroborated broker-flat evidence. Normal
owned records clear after two consecutive zero-position broker polls. Records
that are already `RECONCILING` get one extra confirmation and clear on the third
consecutive zero-position poll. If the same ownership row remains beyond that,
treat it as stale VM code, a persistence failure, or an authority-path mismatch
before using manual recovery.

1. Check the dashboard status contract:

   ```bash
   curl -sS http://localhost:8000/dashboard/status
   ```

   Confirm `readiness.reason` is `position_authority_degraded` and note the
   `readiness.position_state_counts` / `readiness.degraded_scope_count` values.

2. Fetch recovery evidence:

   ```bash
   curl -sS -H "X-Admin-Key: <operator-key>" \
     http://localhost:8000/admin/state/position-authority/recovery
   ```

   Review each `records[]` item. The safe path requires
   `broker_evidence.status = "flat"` and `recovery.allowed_without_force = true`.
   The payload intentionally excludes broker credentials and secrets.

3. Clear the stuck internal record only after broker-flat evidence is present:

   ```bash
   curl -sS -X POST -H "X-Admin-Key: <operator-key>" \
     -H "Content-Type: application/json" \
     -d '{"scope_key":"<scope_key>","reason":"broker_flat_verified_recovery","force":false}' \
     http://localhost:8000/admin/state/clear-position-record
   ```

   The endpoint refuses nonzero broker evidence with HTTP 409. `force=true` is a
   break-glass override and is recorded in the audit event.
   When `force=false` succeeds with `broker_evidence.status = "flat"`, the
   endpoint also attempts to recover the matching in-memory degraded scope. A
   backend restart should not be required just to remove the recovered scope
   from readiness.

4. Recheck readiness from the same workflow:

   ```bash
   curl -sS http://localhost:8000/dashboard/status
   curl -sS http://localhost:8000/readyz
   ```

   Readiness should recover once no degraded scopes or DEGRADED/RECONCILING
   position records remain. If it does not, inspect the clear response's
   `degraded_scope_recovered` / `degraded_scope_recovery_error` fields, fetch
   recovery evidence again, and review the remaining record or degraded scope.
