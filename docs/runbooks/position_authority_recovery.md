# Position Authority Recovery

Use this workflow only when broker/current-position evidence proves the contract
is flat but Phoenix still reports a degraded or reconciling internal position
record.

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

4. Recheck readiness from the same workflow:

   ```bash
   curl -sS http://localhost:8000/dashboard/status
   curl -sS http://localhost:8000/readyz
   ```

   Readiness should recover once no degraded scopes or DEGRADED/RECONCILING
   position records remain. If it does not, fetch recovery evidence again and
   review the remaining record or degraded scope.
