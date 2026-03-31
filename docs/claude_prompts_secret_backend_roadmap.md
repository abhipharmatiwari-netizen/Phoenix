# Claude prompts for Phoenix v9 secret/backend alignment

Use these one at a time.

## 1) Current-state alignment: Postgres + host-injected Windows SecretStore

```text
You are patching Phoenix v9 so the codebase matches the documented current LIVE model.

Current supported LIVE model:
- Broker secrets may come from Postgres (`BROKER_SECRET_BACKEND=postgres`).
- Broker secrets may also come from host-injected Windows SecretStore values, which arrive in the app as env vars (`BROKER_SECRET_BACKEND=env`).
- Windows SecretStore is a host-side operator injection mechanism in this repo, not an in-app Python vault backend.
- The checked-in Docker Desktop path currently uses injected env values.

Patch goals:
1. Remove misleading LIVE terminology that says Secret Manager or Vault is mandatory in the current repo.
2. Align validators, settings descriptions, docs, and tests with the current LIVE model above.
3. Make `docker-compose.live.single.yml`, helper scripts, and startup guidance internally consistent:
   - either document and preserve the injected-env path explicitly,
   - or add an explicit compose/profile switch so operators can choose env vs postgres for broker secrets without hand-editing YAML.
4. Ensure any remaining `vault` label is either removed from current behavior or clearly marked as future scope only.
5. Keep committed env/example files free of real secrets.

Files to inspect first:
- `app/core/startup_config_validator.py`
- `app/config/settings.py`
- `app/brokers/secret_loader.py`
- `docker-compose.live.single.yml`
- `start-docker-secretstore.ps1`
- `start-local.ps1`
- `README.md`
- `ARCHITECTURE.md`
- `ABOUTME.md`
- `docs/runbooks/docker_desktop_live_deployment.md`
- tests under `tests/core/`, `tests/config/`, `tests/brokers/`

Done when:
- docs, validators, tests, and checked-in deployment assets all describe the same current LIVE secret model,
- Windows SecretStore is described as host injection rather than an in-app backend,
- operators can follow one coherent Docker Desktop path without hidden assumptions.
```

## 2) Future scope: Google Secret Manager + Cloud Run

```text
Design and implement the next Phoenix v9 scale target:
- deployable Cloud Run path,
- Google Secret Manager as the LIVE secret source for that path.

Requirements:
1. Add a Cloud Run deployment profile and operator documentation.
2. Implement/finish an application-level `secret_manager` backend for broker/runtime/admin secrets.
3. Keep current Windows/Docker Desktop path intact.
4. Update startup validation so Cloud Run + Secret Manager is a first-class approved LIVE path.
5. Add tests for backend selection, secret resolution, and Cloud Run profile linting.

Deliver:
- changed files,
- deployment steps,
- known operational prerequisites.
```

## 3) Future scope: Firestore-backed broker secret storage

```text
Add Firestore as a broker-secret storage backend for Phoenix v9.

Requirements:
1. Create a pluggable backend in `app/brokers/secret_loader.py` for Firestore.
2. Define secret document schema, environment scoping, rotation metadata, and audit hooks.
3. Keep current Postgres and injected-env paths working.
4. Update settings, validators, docs, and tests.
5. Fail closed in LIVE on missing/ambiguous Firestore secret records.

Deliver:
- schema/doc design,
- implementation,
- targeted tests,
- migration/operator notes.
```

## 4) Future scope: BigQuery expansion / Postgres replacement analysis

```text
Prepare Phoenix v9 for a future BigQuery-backed storage path.

Requirements:
1. Audit every current Postgres-authoritative responsibility:
   - order outbox,
   - position ownership,
   - order lifecycle,
   - sweep/EOD state,
   - kill-switch durability,
   - indicator/trade persistence.
2. For each area, classify whether BigQuery can safely replace Postgres directly, should remain analytics-only, or needs an intermediate service/queue.
3. Do not weaken current LIVE guarantees around idempotency, mutation serialization, or fail-closed startup.
4. Produce a phased design and only implement the parts that are safe without changing authoritative behavior unexpectedly.

Deliver:
- architecture decision record,
- phased implementation plan,
- any safe initial code abstractions.
```

## 5) Final consistency pass

```text
You have completed the requested Phoenix v9 secret/backend changes.
Do a final consistency pass across code, docs, examples, scripts, and tests.

Check for stale references to:
- Secret Manager or Vault as the current mandatory LIVE path,
- Windows SecretStore presented as an in-app backend,
- compose/runbook steps that no longer match real startup behavior,
- outdated example env values.

Run the tightest relevant test subsets, summarize changed files, and call out any manual operator follow-up still required.
```
