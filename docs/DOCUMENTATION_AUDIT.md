# Documentation Audit - 2026-05-07

## Executive Summary

Documentation readiness: **GO** after this cleanup. The current operator docs now describe the same production contract: hub-authoritative LIVE, Postgres durable authority, stream-worker-enabled automated runtime, approved platform secret injection, and release evidence as the go-live gate.

Production readiness: **NO-GO for unattended live-money operation**. The docs now expose the remaining implementation gaps instead of hiding them: LIVE break-glass flatten requires a step-up token but the repo has no HTTP issuer route, and kill-switch rearm does not enforce step-up.

## Documentation Inventory

| Path | Type | Current purpose | Owner/audience | Status | Action |
|---|---|---|---|---|---|
| `README.md` | Operator index | Current runtime contract and doc map | Operators, maintainers | PARTIALLY_STALE | UPDATE |
| `ABOUTME.md` | Plain-language summary | Non-authoritative orientation | New operators, reviewers | PARTIALLY_STALE | UPDATE |
| `ARCHITECTURE.md` | Production contract | Authoritative runtime and safety contract | Architects, SRE, reviewers | PARTIALLY_STALE | UPDATE |
| `docs/DOCUMENTATION_AUDIT.md` | Audit record | Inventory, mismatches, validation | Maintainers, reviewers | CURRENT | KEEP |
| `docs/Flowchart.md` | Reference diagram | Runtime flow diagram, non-authoritative | Engineers | PARTIALLY_STALE | UPDATE |
| `docs/kpis_slos.md` | Observability guide | Current metrics, alerts, day-1 monitor set | SRE, operators | CONFLICTS_WITH_CODE | UPDATE |
| `docs/parameters.md` | Research reference | Historical strategy parameter research | Strategy reviewers | ROADMAP_ONLY | UPDATE |
| `docs/nse-holidays.txt` | Scheduler input | OCI weekday holiday guard | Operators | CONFLICTS_WITH_CODE | UPDATE |
| `docs/archive/ARCHIVE.md` | Archive index | Explains archive scope | Maintainers | CURRENT | KEEP |
| `docs/archive/phoenix_backlog.csv` | Historical backlog | Development history only | Maintainers | OBSOLETE | ARCHIVE |
| `docs/release-evidence/README.md` | Evidence guide | How to capture evidence bundles | Operators | PARTIALLY_STALE | UPDATE |
| `docs/release-evidence/restore_drill_TEMPLATE.md` | Evidence template | Restore drill record template | Operators | CURRENT | KEEP |
| `docs/release-evidence/restore_drill_20260425.md` | Evidence artifact | Completed historical drill | Auditors | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/docker_desktop_live_deployment.md` | Runbook | Docker Desktop LIVE deployment | Operators | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/oci_live_deployment.md` | Runbook | OCI Compose LIVE deployment | Operators | CONFLICTS_WITH_CODE | UPDATE |
| `docs/runbooks/cloud_run_live_deployment.md` | Reference | Future Cloud Run path | Architects | ROADMAP_ONLY | UPDATE |
| `docs/runbooks/release_evidence.md` | Runbook | Promotion evidence standard | Release operators | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/update_broker_credentials.md` | Runbook | Postgres broker credential rotation | Operators | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/blue_green_cutover.md` | Runbook | Controlled writer handoff | Operators, SRE | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/restore_drill.md` | Runbook | Backup/restore validation | SRE, operators | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/kill_switch.md` | Runbook | Trip, clear, rearm workflow | Operators | UNSAFE_FOR_LIVE | UPDATE |
| `docs/runbooks/break_glass_flatten.md` | Runbook | Emergency flatten workflow | Operators | UNSAFE_FOR_LIVE | UPDATE |
| `docs/runbooks/resolve_orphan_review.md` | Runbook | Orphan review resolution | Operators | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/capital_limits_configuration.md` | Runbook | Capital limit payloads | Operators | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/strategy_runtime_diagnostics.md` | Diagnostic runbook | Stream/strategy troubleshooting | Operators, SRE | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/ema20_tp_pct_tuning.md` | Research runbook | Future tuning workflow | Strategy reviewers | ROADMAP_ONLY | UPDATE |
| `docs/runbooks/cloudrun-live.env.example` | Env template | Cloud Run reference profile | Architects | ROADMAP_ONLY | UPDATE |
| `docs/runbooks/oci-live.env.example` | Env template | OCI non-secret deploy env template | Operators | PARTIALLY_STALE | UPDATE |
| `docs/runbooks/docker-live.env.example` | Env template | Obsolete multi-file Docker LIVE profile | Operators | OBSOLETE | DELETE |
| `.env.example` | Env template | Local/dev example | Developers | CURRENT | KEEP |
| `docker.env` | Env template | Local SHADOW/dev template | Developers | CURRENT | KEEP |
| `cloudrun.env` | Env template | Cloud Run reference only | Architects | ROADMAP_ONLY | UPDATE |
| `.env.oci-live.example` | Env template | Duplicate OCI env template | Operators | DUPLICATE | DELETE |
| `docker-compose.live.single.yml` | Compose manifest | Docker Desktop LIVE manifest | Operators | CURRENT | KEEP |
| `docker-compose.oci-live.yml` | Compose manifest | OCI Compose LIVE manifest | Operators | CURRENT | KEEP |
| `phoenix-override.yml.example` | Compose override | OCI operator override template | Operators | CONFLICTS_WITH_CODE | UPDATE |
| `Dockerfile` | Build config | Backend image build | Maintainers | CURRENT | KEEP |
| `nginx/nginx.conf.template` | Nginx template | Local/reverse-proxy config | Operators | CURRENT | KEEP |
| `nginx/nginx-ssl.conf.template` | Nginx template | OCI TLS reverse proxy config | Operators | CURRENT | KEEP |
| `release-manifest.json` | Release manifest | Promotion artifact policy | Release operators | PARTIALLY_STALE | UPDATE |
| `scripts/build_release_artifact.py` | Release script | Source bundle builder | Maintainers | PARTIALLY_STALE | UPDATE |
| `scripts/start-phoenix.sh` | Ops script | OCI scheduled start | Operators | CONFLICTS_WITH_CODE | UPDATE |
| `scripts/stop-phoenix.sh` | Ops script | OCI scheduled stop | Operators | CONFLICTS_WITH_CODE | UPDATE |
| `scripts/fetch-secrets.sh` | Ops script | OCI Vault to `/run/secrets` | Operators | CURRENT | KEEP |
| `scripts/docker-entrypoint.sh` | Startup script | Loads Docker secrets to env | Operators | CURRENT | KEEP |
| `scripts/capture_release_evidence.ps1` | Evidence script | Captures evidence JSON | Operators | CURRENT | KEEP |
| `scripts/run_migrations.sh` | Migration script | Apply/verify schema migrations | Operators | CURRENT | KEEP |
| `scripts/rollback.py` | Utility | Rollback helper | Maintainers | CURRENT | KEEP |
| `scripts/replay/REPLAY.md` | Replay docs | Replay subsystem usage | Developers | CURRENT | KEEP |
| `scripts/ops/build_push_ip.sh` | OCI script | Instance-principal OCIR build/push | Operators | PARTIALLY_STALE | UPDATE |
| `scripts/ops/build_and_push_image.sh` | OCI script | Token-based OCIR build/push | Operators | PARTIALLY_STALE | UPDATE |
| `scripts/ops/pull_oci_logs.ps1` | OCI script | Bastion log capture | Operators | PARTIALLY_STALE | UPDATE |
| `scripts/ops/gather_ocir_config.sh` | OCI script | Read-only OCIR config capture | Operators | CURRENT | KEEP |
| `scripts/ops/check_attribution_deployed.sh` | OCI script | Read-only attribution marker check | Operators | CURRENT | KEEP |
| `scripts/ops/analyze_exit_attribution.py` | Research script | Exit attribution analysis | Strategy reviewers | ROADMAP_ONLY | KEEP |
| `scripts/ops/analyze_tp_discrimination.py` | Research script | TP discrimination analysis | Strategy reviewers | ROADMAP_ONLY | KEEP |
| `scripts/ops/manual_deploy.sh` | OCI script | Old bind-mount deploy path | Operators | OBSOLETE | DELETE |
| `scripts/ops/build_local_and_redeploy.sh` | OCI script | Old local-image deploy path | Operators | OBSOLETE | DELETE |
| `scripts/ops/resolve_stuck_order.sh` | OCI script | Hard-coded direct DB state mutation | Operators | UNSAFE_FOR_LIVE | DELETE |
| `scripts/ops/check_outbox.sh` | OCI script | Old local-Postgres read helper | Operators | OBSOLETE | DELETE |
| `scripts/ops/backtest_smoke.sh` | Research script | Old live-DB replay smoke | Strategy reviewers | OBSOLETE | DELETE |

## Evidence Review Mismatches

| Severity | Document | Claim | Evidence from repo | Risk | Required change |
|---|---|---|---|---|---|
| P0 | `docs/runbooks/break_glass_flatten.md`, `app/dashboard/admin_routes.py` | LIVE break-glass can be run by following the runbook | `BreakGlassFlattenRequest` requires `step_up_token` in LIVE, but no HTTP issuer route exists; only `app/security/step_up.py` service functions exist | Operator may believe emergency flatten is executable when token issuance is not wired | Mark not approved for LIVE unless token is issued through approved process; remove nonexistent issuer route claim |
| P0 | `docs/runbooks/kill_switch.md` | Rearm requires step-up/maker-checker in app | `/admin/kill-switch/rearm` requires OPERATOR role only | Trading can be re-enabled without the architecture-required step-up | Document current gap and require external maker-checker until implemented |
| P1 | Multiple admin runbooks | `Authorization: Bearer <ADMIN_API_KEY>` authenticates admin API | `app/dashboard/auth.py` accepts admin key via `X-Admin-Key`; bearer is JWT auth path | Runbook commands fail or encourage wrong auth model | Replace with `X-Admin-Key` |
| P1 | `docs/runbooks/oci_live_deployment.md`, `phoenix-override.yml.example` | OCI uses local `phoenix-oci-postgres`, SSL skip, and temporary source bind mounts | `docker-compose.oci-live.yml` expects external Postgres endpoint, OCIR image tags, `/run/secrets` | Failed or unsafe OCI deployment; unpinned code overlays | Rewrite OCI runbook and override template for external Postgres, SSL, pinned images, no source mounts |
| P1 | `scripts/start-phoenix.sh`, `scripts/stop-phoenix.sh` | Compose service `web` exists | OCI compose service is `nginx`; container is named `phoenix-oci-web` | Scheduled starts/stops fail | Change service operations to `nginx`; remove market-open `git pull` |
| P1 | `docs/runbooks/docker-live.env.example` | Legacy Docker LIVE env template is current | Referenced compose files are absent; current manifest is `docker-compose.live.single.yml` | Operators may deploy nonexistent/stale profile | Delete obsolete template and references |
| P1 | `.env.oci-live.example` | Root OCI env template is current | Canonical template is `docs/runbooks/oci-live.env.example` | Duplicate drift | Delete duplicate |
| P1 | `scripts/ops/resolve_stuck_order.sh` | Force-terminal direct DB update is safe | Script hard-coded local container and mutates order outbox | Money-movement state can be changed outside current runbook controls | Delete |
| P1 | `scripts/ops/build_push_ip.sh`, `scripts/ops/build_and_push_image.sh` | OCIR user/namespace can be hard-coded | Scripts embedded tenancy/user assumptions | Secret/account leakage and wrong tenancy push | Require `OCIR_NAMESPACE`/`OCIR_USERNAME` env vars |
| P2 | `docs/kpis_slos.md` | Lists many metric names as supported | Actual metrics are in `app/observability/prometheus_metrics.py` and alert rules in `alert_rules.py` | Operators monitor nonexistent metrics | Replace with code-proven metrics and day-1 alert rules |
| P2 | `docs/parameters.md`, `docs/runbooks/ema20_tp_pct_tuning.md` | Backtest recommendations read as current LIVE params | Current runnable params are in `app/config/strategy_env.yaml` and runtime DB config | Research may be applied as production truth | Mark research/reference only |
| P2 | `cloudrun.env`, Cloud Run docs | Cloud Run profile read as deployable | No repo-tracked Cloud Run manifest/release evidence; architecture says roadmap | Premature Cloud Run go-live | Mark roadmap/reference only |
| P2 | `docs/nse-holidays.txt` | 2026 holiday dates current | NSE holiday page and current market calendars differ from file | Scheduled OCI start can run on holidays or skip trading days | Correct dates and mark verification date |

## Clean Documentation Map

| Final document | Purpose | Must contain | Must not contain | Source documents |
|---|---|---|---|---|
| `ARCHITECTURE.md` | Authoritative production contract | LIVE tuple, authority model, storage rules, security contract, known implementation gaps | Go-live shortcuts or future-path approvals without evidence | Existing architecture plus code/config review |
| `README.md` | Concise operator index | Current runtime contract, deployment surfaces, reading order | Detailed runbook steps, obsolete profile instructions | README, architecture, runbooks |
| `ABOUTME.md` | Plain-language summary | Non-authoritative explanation and reading order | Operational authority or conflicting go-live guidance | ABOUTME, architecture |
| `docs/runbooks/docker_desktop_live_deployment.md` | Docker Desktop LIVE runbook | Preconditions, commands, env, validation, rollback, warnings | OCI/Cloud Run steps, legacy multi-file compose | Docker compose, helper script, startup validator |
| `docs/runbooks/oci_live_deployment.md` | OCI Compose LIVE runbook | OCIR image, Vault secrets, external Postgres, migrations, `/readyz`, release evidence, rollback | VM-local Postgres, source bind mounts, `latest` tags | OCI compose, override, scripts |
| `docs/runbooks/release_evidence.md` | Approval evidence standard | Required fields, pass criteria, Docker/OCI capture commands, failure handling | Claims that evidence proves strategy suitability | Runtime evidence endpoint |
| Emergency runbooks | Controlled manual actions | Purpose, scope, auth header, failure handling, rollback | Unwired step-up claims or direct DB repairs as routine | Admin routes and auth code |
| Reference docs | Research/roadmap | Clear status and source of truth | Current go-live claims | Existing research and roadmap files |

## Delete And Archive Actions

| Path | Action | Reason | Replacement / canonical reference |
|---|---|---|---|
| `.env.oci-live.example` | DELETE | Duplicate root OCI env template drifted from the runbook copy | `docs/runbooks/oci-live.env.example` |
| `docs/runbooks/docker-live.env.example` | DELETE | Obsolete multi-file Docker profile references absent compose files | `docker-compose.live.single.yml`, `docs/runbooks/docker_desktop_live_deployment.md` |
| `scripts/ops/manual_deploy.sh` | DELETE | Old source bind-mount deployment path conflicts with pinned image contract | `docs/runbooks/oci_live_deployment.md` |
| `scripts/ops/build_local_and_redeploy.sh` | DELETE | Old local-image and bind-mount repair path conflicts with OCIR/pinned-image deployment | `scripts/ops/build_and_push_image.sh`, `scripts/ops/build_push_ip.sh` |
| `scripts/ops/resolve_stuck_order.sh` | DELETE | Hard-coded direct DB mutation for order state is unsafe for LIVE | Incident-specific operator approval plus current runbooks |
| `scripts/ops/check_outbox.sh` | DELETE | Hard-coded VM-local Postgres container no longer matches repo-tracked OCI path | `/readyz`, release evidence, SQL through approved DB access |
| `scripts/ops/backtest_smoke.sh` | DELETE | Old research helper connected to live local Postgres container and installed deps dynamically | `docs/runbooks/ema20_tp_pct_tuning.md`, replay tooling |

No document was archived in this pass; deleted files had no safe current operational value.

## Files Updated

Updated: `README.md`, `ABOUTME.md`, `ARCHITECTURE.md`, `cloudrun.env`, `docs/Flowchart.md`, `docs/kpis_slos.md`, `docs/parameters.md`, `docs/nse-holidays.txt`, `docs/release-evidence/README.md`, `docs/release-evidence/restore_drill_20260425.md`, all current runbooks under `docs/runbooks/`, `phoenix-override.yml.example`, `release-manifest.json`, `scripts/build_release_artifact.py`, `scripts/start-phoenix.sh`, `scripts/stop-phoenix.sh`, `scripts/ops/build_push_ip.sh`, `scripts/ops/build_and_push_image.sh`, `scripts/ops/pull_oci_logs.ps1`, `app/dashboard/admin_routes.py`, `app/config/profile_linter.py`, and the two env-template tests.

## Key Content Changes

- README and ABOUTME now point to the same current runtime contract as ARCHITECTURE.
- Cloud Run is consistently roadmap/reference, not current go-live guidance.
- Docker Desktop and OCI Compose are the only current repo-tracked deployment surfaces.
- Admin runbooks now use `X-Admin-Key` and explicitly call out the step-up issuer gap.
- OCI docs and override template now require external Postgres, SSL, Vault/file secrets, OCIR images, and pinned tags.
- Obsolete direct-DB and bind-mount scripts were deleted.
- Release artifact policy now includes the OCI compose manifest and override template.
- KPI/SLO docs now list metrics and alert rules that exist in code.
- 2026 NSE holiday scheduler file was corrected and dated.

## Remaining Documentation And Production Risks

- Production code gap: no HTTP step-up issuer route for LIVE break-glass flatten.
- Production code gap: kill-switch rearm lacks step-up enforcement.
- Cloud Run remains unapproved until a manifest, secret binding, runtime evidence, and recovery evidence exist.
- Full test suite currently fails in unrelated runtime/test areas; targeted documentation/config tests pass.
- Raw recursive doc enumeration is noisy because `.venv`, `.backtest_venv`, and `replay_output` live inside the workspace.
- `docs/Flowchart.md` still contains Firestore compatibility nodes; the status banner says they are not current go-live guidance.

## Validation Commands Executed

| Command | Result |
|---|---|
| `git status --short` | Completed; shows expected doc/script changes plus pre-existing git ignore permission warnings |
| `Get-ChildItem -Recurse -File -Include *.md,*.env,*.yml,*.yaml` | Non-clean raw listing due workspace venv/replay-output noise; scoped `rg --files` listing completed |
| Required grep equivalent for runtime/deployment terms with `rg` | Completed; remaining hits are intentional current/roadmap warnings |
| Required grep equivalent for secret terms with `rg` | Completed; remaining hits are placeholders, secret-source rules, or code/config secret mounts |
| `pytest -q tests/config/test_profile_linter.py tests/security/test_sprint1_security.py tests/test_release_manifest_lint.py tests/test_live_single_stack_profile.py` | PASS: 49 passed, 2 warnings |
| `pytest -q` | FAIL: 1754 passed, 20 failed, 2 skipped, 856 warnings; failures are in runtime readiness/startup validator and persistence tests, not markdown/link checks |
| `python -m json.tool release-manifest.json` | PASS |
| Markdown relative link check | PASS: all relative links resolve |
| `docker compose -f docker-compose.live.single.yml config --quiet` with dummy non-secret env | PASS; Docker emitted `config.json: Access is denied` warning |
| `docker compose -f docker-compose.oci-live.yml -f phoenix-override.yml.example config --quiet` with dummy non-secret env | PASS; Docker emitted `config.json: Access is denied` warning |
| `git diff --stat`, `git diff --name-status` | Completed |

## Final Operator Reading Order

1. `ARCHITECTURE.md`
2. `README.md`
3. `ABOUTME.md`
4. `docs/runbooks/docker_desktop_live_deployment.md` or `docs/runbooks/oci_live_deployment.md`
5. `docs/runbooks/release_evidence.md`
6. `docs/runbooks/update_broker_credentials.md`
7. `docs/runbooks/capital_limits_configuration.md`
8. `docs/runbooks/blue_green_cutover.md`
9. `docs/runbooks/restore_drill.md`
10. `docs/kpis_slos.md`
11. Emergency only: `docs/runbooks/kill_switch.md`, `docs/runbooks/resolve_orphan_review.md`, `docs/runbooks/break_glass_flatten.md`
12. Reference only: `docs/runbooks/cloud_run_live_deployment.md`, `docs/parameters.md`, `docs/Flowchart.md`

## Git Diff Summary

`git diff --stat` after the cleanup reported 44 tracked changed paths, 714 insertions, and 1514 deletions, plus this new untracked audit file. The largest reductions came from replacing the stale OCI runbook and deleting obsolete env/script paths.
