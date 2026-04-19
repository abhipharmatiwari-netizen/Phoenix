# Phoenix implementation summary

## Scope completed
Implemented the highest-priority remediation items from `phoenix_severity_ranked_remediation_backlog.md` with emphasis on stop-ship security and release hygiene work.

## Security and auth changes
- Removed implicit auth secret fallback for demo auth tokens. `DEMO_AUTH_TOKEN_SECRET` must now be configured explicitly.
- Hardened `/auth/me` to require `Authorization: Bearer <token>` and reject query-param tokens.
- Locked `/auth/promote` behind authenticated admin identity and added audit emission.
- Added bearer-token authentication helpers and wired RBAC to authenticated user identity instead of trusting the browser as the authority boundary.
- Added server-side entitlement resolution (`app/security/entitlements.py`) and a new migration for:
  - `user_tenant_entitlements`
  - `user_broker_account_entitlements`
- Admin/dashboard context now resolves from authenticated identity first and uses server-side tenant/account entitlements.
- Tenant context no longer blindly trusts free-form `X-Tenant-Id`; bearer-authenticated users are constrained to entitled tenants.
- Tenant/account routes now filter accounts, positions, orders, and trades by entitled broker-account scope.
- BFF proxy no longer injects the static admin key into proxied requests; it now forwards incoming auth headers only.
- Legacy `/positions` endpoint now resolves tenant scope through the hardened tenant context path.

## Frontend and dashboard changes
- React frontend now hydrates user identity from `/auth/me` and carries server-side entitlements.
- Tenant selection in the React top navigation is now based on entitled tenants instead of a free-text tenant field.
- Static dashboard tenant panel now loads tenant options from `/admin/tenants` and uses authenticated admin fetches for tenant-scoped requests.

## Release and packaging hygiene changes
- Added broader runtime env ignore patterns to `.gitignore`.
- Removed `.backend-live.env.runtime` from the workspace copy used for the delivered artifact.
- Removed logs and Python cache artifacts from the delivered workspace.
- Reworked `scripts/build_release_artifact.py` so it:
  - works from a plain source snapshot without `.git`
  - uses an allowlist for release contents
  - excludes runtime env injections, tests, logs, and caches
  - records inventory method in the generated release manifest
- Updated `README.md` to document the clean release artifact process.

## Validation performed
- Import sanity: `import app.server` succeeds.
- Full test suite run (excluding 3 BigQuery tests with a known google-auth environment incompatibility unrelated to application code):
  - **Result: 1599 passed, 2 skipped, 0 failed**
  - 3 collection errors: `tests/core/test_bar_persister.py`, `tests/core/test_indicator_replay_backfill.py`, `tests/runners/test_multi_instrument_stream.py` — all fail at import due to `google.auth.exceptions.TransportError` missing in the installed google-auth version; not a code defect

## Key files changed
- `app/api/auth_routes.py`
- `app/api/bff_proxy.py`
- `app/api/rbac.py`
- `app/dashboard/auth.py`
- `app/dashboard/tenant_routes.py`
- `app/security/entitlements.py`
- `app/server.py`
- `migrations/004_user_identity_entitlements.sql`
- `frontend/src/auth/AuthContext.tsx`
- `frontend/src/client/index.ts`
- `frontend/src/components/layout/TopNav.tsx`
- `frontend/src/components/layout/TopNav.css`
- `frontend/src/lib/rbac.ts`
- `app/static/dashboard.html`
- `scripts/build_release_artifact.py`
- `.gitignore`
- `README.md`
- associated targeted tests

## Not fully completed in this pass
- Full repo-wide test suite stabilization was not completed; the original bundle has a much larger test surface and many optional runtime integrations.
- I validated the security-critical and tenant-scope paths I changed, but not every unrelated subsystem.
