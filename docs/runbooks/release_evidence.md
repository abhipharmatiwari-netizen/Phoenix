# LIVE Release Evidence — Operator Approval Standard

**Architecture reference:** LIVE deployment safety, §12, §19.3

---

## Purpose

Before approving a new LIVE backend release, the operator on duty must collect and
review a release-evidence bundle that proves the container started safely. Releasing
without this evidence is explicitly forbidden.

## Scope

This runbook applies to the current OCI VM deployment. Docker Desktop and Cloud
Run material are non-current for production unless a future OCI VM audit proves
that deployment model is active. This runbook does not approve any deployment
whose `/admin/release-evidence` output cannot be captured from the deployed
backend.

## Preconditions

- The backend is deployed through a current runbook.
- `ADMIN_API_KEY` is available from the approved operator secret process, not from a repo env file.
- `/readyz` is reachable through the deployment endpoint.

---

## Collecting the bundle

```powershell
curl.exe -s -H "X-Admin-Key: $env:ADMIN_API_KEY" `
    http://localhost/admin/release-evidence | python -m json.tool
```

The endpoint returns a structured JSON object. Capture it and attach it to the
deployment log or PR.

---

## Required evidence fields and pass criteria

| Field | Pass condition |
|---|---|
| `trade_mode` | `"LIVE"` |
| `runtime_ready` | `true` |
| `is_leader` | `true` |
| `position_authority_restored` | `true` when `startup_recovery.summary.position_records_loaded > 0`; `false` is allowed only for a fresh database with zero non-terminal position records and green `/readyz` effective authority |
| `schema_guard.status` | `"ok"` or empty missing lists |
| `startup_recovery.status` | `"ok"` or `"skipped"` — never `"failed"` |
| `stream_worker.running` | `true` |
| `runner_count` | ≥ 1 |
| `kill_switch.active_count` | `0` (no tripped kill switches) |
| `safety_flags.enable_capital_checks` | `true` |
| `safety_flags.enable_risk_checks` | `true` |
| `safety_flags.order_submission_outbox_required` | `true` |
| `safety_flags.position_ownership_enabled` | `true` |
| `safety_flags.risk_fail_open_on_missing_pnl` | `false` |
| `synthetic_contamination_cleared` | `true` |

Any field outside pass criteria is a **deployment blocker**. Do not approve release
until every item passes.

> **`position_authority_restored` on a fresh database:** On first deployment or after
> a clean database restore with no prior session history, `position_authority_restored`
> will be `false` because no position records exist in Postgres. This is expected
> behavior and is not a blocker on initial deployment. Record this explicitly in the
> deployment notes. On any subsequent deployment after positions have been written,
> `false` is a blocker that requires investigation.

---

## Pre-deploy smoke test

Before deploying, run the lightweight smoke suite to verify import integrity and config lint:

```bash
pytest -m smoke -q
# Expected: all tests pass in < 30 seconds
# This replaces the deleted backtest_smoke.sh
```

The smoke suite covers:
- All critical module imports succeed
- Settings instantiate without error
- Config profiles pass the profile linter
- `/readyz` and `/health` endpoint schemas are stable
- Startup validator rejects known-bad configurations

A smoke failure is a deployment blocker: fix the root cause before proceeding.

---

## Evidence collection procedure

1. Run `pytest -m smoke -q` — must pass.
2. Deploy the new backend image via the active deployment runbook.
3. Wait for the health check to pass (`docker compose ps` → `healthy`).
4. Collect the evidence bundle:
   ```powershell
   $bundle = curl.exe -s -H "X-Admin-Key: $env:ADMIN_API_KEY" `
       http://localhost/admin/release-evidence | ConvertFrom-Json
   $bundle | ConvertTo-Json -Depth 10
   ```
5. Review each field against the pass criteria table above.
6. If all criteria pass, record the `generated_at` timestamp in the deployment log.
7. If any criterion fails, roll back or hold the stack stopped and investigate before re-attempting.

OCI equivalent:

```bash
ADMIN_KEY="$(sudo cat /run/secrets/admin_api_key)"
curl -sk -H "X-Admin-Key: ${ADMIN_KEY}" \
  https://127.0.0.1:8443/admin/release-evidence
```

---

## Attaching to PR / deployment record

Paste the full JSON output as a code block in:
- The GitHub PR description, **or**
- The deployment record in your incident/change management system.

The `generated_at` timestamp must be within 10 minutes of the deployment approval
timestamp.

---

## What the bundle does NOT prove

- That strategies are correctly configured for today's market conditions.
- That broker credentials are valid (verify separately via `/health` broker sync timestamps).
- That capital limits are calibrated correctly (verify via `CAPITAL_LIMITS_JSON` review).

---

## Related

- [OCI LIVE Deployment](oci_live_deployment.md)
- [Kill Switch](kill_switch.md)
- [Break-Glass Flatten](break_glass_flatten.md)
