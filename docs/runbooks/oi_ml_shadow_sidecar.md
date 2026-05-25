# OI/ML Shadow Sidecar Runbook

Status: current progress record for the OI/ML CE seller shadow sidecar as of
2026-05-23 IST.

This sidecar is a dry-run research and validation process. It must not place,
modify, cancel, or exit live orders. It runs beside the live OCI stack, publishes
no host ports, writes normalized option-chain snapshots and shadow order intents
to Postgres, and keeps `OI_ML_SHADOW_ALLOW_NAKED=false`.

## Current State

| Area | State |
|---|---|
| Branch | `oi-ml-shadow-sidecar` |
| Latest deployed sidecar commit | `bd999cd` |
| OCI checkout | `/opt/phoenix/oi-ml-shadow-src` |
| Compose file | `/opt/phoenix/oi-ml-shadow.yml` |
| Running image | `phoenix-oi-ml-shadow:oi-ml-shadow-bd999cd` |
| Container | `phoenix-oi-ml-shadow` |
| Database tables | `public.option_chain_1m`, `public.oi_ml_shadow_order_intents`, `public.option_chain_validation_reports` |
| Default scorer | `missing` in compose; deployed smoke override uses `constant` |
| Smoke scorer currently used | `OI_ML_SHADOW_SCORER=constant`, probability `0.64`, MAE premium `40` |
| Broker proxy/session | Sidecar now forwards backend broker proxy env and reuses the Angel quote session during the snapshotter session |
| LightGBM support | Implemented with `lightgbm==4.6.0`; artifact paths must be explicit |
| Continuous NSE validation | Enabled in deployed sidecar; reports stored in `public.option_chain_validation_reports` |
| Snapshot validation window | `09:15`-`15:30` IST; trade-decision window remains `09:45`-`14:30` IST |
| Live order routing | Not used by the sidecar |

Recent validation:

- Local focused OI/ML suite: `159 passed`.
- 2026-05-25 live backend/nginx deployment moved the main VM checkout to
  `e7f1e29` with backend/nginx images tagged `local-e7f1e29`. The sidecar
  image remains `phoenix-oi-ml-shadow:oi-ml-shadow-bd999cd` and remains
  dry-run only.
- 2026-05-20 16:50 UTC / 22:20 IST rectification deployment:
  backend `phoenix-local-backend:local-e1f9ddb`, nginx
  `phoenix-local-nginx:local-349d55f`, sidecar
  `phoenix-oi-ml-shadow:oi-ml-shadow-50513ec`.
- 2026-05-23 00:12 IST NSE-validation fix deployment: sidecar image
  `phoenix-oi-ml-shadow:oi-ml-shadow-bd999cd` is healthy. From inside the
  sidecar, the classic NSE option-chain API returned no usable reference rows,
  while the live-derivatives fallback returned `288` NIFTY `2026-05-26`
  reference rows. A read-only compare against the latest stored Angel snapshot
  showed `814` Angel rows, `288` NSE fallback rows, `288` compared contracts,
  `526` Angel-only contracts, `0` NSE-only contracts, and `215` common-contract
  mismatches. Remaining mismatch fields were OI, LTP, and volume; IV/bid/ask
  were skipped because the fallback endpoint does not publish them.
- Post-rectification `/readyz` returned `ready=true`, backend/nginx were
  healthy, and `/health/summary` reported `status=degraded` with
  `oi_ml_shadow_ingestion_degraded` because 2026-05-20 IST sidecar evidence was
  absent.
- Post-deploy `/readyz` returned `ready=true` through both backend-local and
  nginx-local checks. Sidecar restarted outside the market window and logged
  `reason=outside_shadow_window`.
- Latest sidecar startup resolved provider-listed NIFTY expiry from Angel scrip
  master: `calendar_default=2026-05-21 listed=2026-05-26`.
- Live backend and nginx were healthy after restart: `/readyz=200`, `/health=200`.
- Off-market smoke on 2026-05-18 21:11 IST proved Angel login through proxy and
  FULL/LTP quote calls: `220` NIFTY rows fetched/stored, `0` shadow intents
  because scorer was fail-closed for the smoke run.
- Off-market validation smoke on 2026-05-18 22:13 IST proved automatic
  NSE-validation report persistence: `report_id=1`, `220` Angel rows,
  `0` NSE comparable rows, status `MISMATCH/WARN`. This old zero-reference
  condition is no longer treated as a provider mismatch: empty reference pulls
  now persist `ERROR/ERROR`, and NIFTY validation falls back to NSE
  live-derivatives rows when available.
- 2026-05-21 live investigation found the sidecar had blank proxy env values
  because compose-time interpolation overrode `/opt/phoenix/phoenix-deploy.env`.
  The compose file now relies on `env_file` for `ANGEL_HTTPS_PROXY` and
  `HTTPS_PROXY`; restart with `--env-file /opt/phoenix/phoenix-deploy.env` so
  image tags and non-secret defaults are also interpolated from the deploy env.

## Implemented Progress

| Slice | Files | Status |
|---|---|---|
| Normalized option quote contract | `app/data/option_chain_provider.py` | Done |
| Angel FULL quote provider adapter | `app/data/angel_option_chain_provider.py` | Done, now also fetches NIFTY spot and India VIX context quotes |
| One-minute snapshotter and Postgres sink | `app/data/oi_snapshotter.py`, `app/data/option_chain_store.py` | Done |
| Backfill parser entrypoint | `app/data/option_chain_backfill.py`, `scripts/data/backfill_option_chain.py` | Done |
| OI feature builder | `app/features/oi_features.py` | Done |
| Intraday labels and dataset builder | `app/strategies/oi_ml/labels.py`, `app/strategies/oi_ml/dataset.py` | Done |
| Runtime scorer contracts | `app/strategies/oi_ml/scoring.py` | Done |
| LightGBM shadow scorer mode | `app/strategies/oi_ml/shadow_runner.py` | Done |
| Shadow order-intent lifecycle | `app/strategies/oi_ml/order_intents.py`, `app/strategies/oi_ml/shadow_lifecycle.py` | Done |
| Sidecar compose | `ops/compose/docker-compose.oi-ml-shadow.yml` | Done |
| Broker proxy/session reuse | `ops/compose/docker-compose.oi-ml-shadow.yml`, `app/data/oi_snapshotter_runtime.py` | Done |
| Provider-filter SQL typing | `app/data/option_chain_repository.py` | Done |
| NSE web validation adapter | `app/data/nse_option_chain_provider.py`, `app/data/option_chain_validation.py`, `scripts/data/validate_nse_vs_angel_option_chain.py` | Done, validation-only; NIFTY falls back to NSE live-derivatives rows if the classic option-chain JSON is empty |
| Continuous NSE validation loop | `app/data/option_chain_realtime_validator.py`, `app/data/option_chain_validation_store.py`, `migrations/023_option_chain_validation_reports.sql` | Done, opt-in via env |
| Read-time NSE IV enrichment | `app/data/option_chain_repository.py` | Done, exact-contract enrichment for Angel reads from recent `provider='nse_web'` rows |
| Strategy scaffold | `app/strategies/oi_ml_ce_seller.py` | Done, fail-closed and disabled by default |
| Phase-0 data-source quality gate | `app/data/option_chain_quality_gate.py`, `scripts/data/report_option_chain_quality.py`, `docs/runbooks/oi_ml_data_source_approval.md` | Done, report blocks promotion until source approval and hard-field completeness pass |
| Spread-aware labels and no-lookahead features | `app/features/oi_features.py`, `app/strategies/oi_ml/backtest.py` | Done, includes source/ingest timestamp lineage, bid/ask quality, OI velocity, wall persistence, beta, and EOD-capped spread labels |
| Offline model promotion gates | `app/strategies/oi_ml/model.py`, `scripts/ml/train_mae_filter.py`, `scripts/ml/walk_forward_oi_ce.py` | Done, walk-forward report remains paper-review only even when gates pass |
| Protected-first runtime entry and exits | `app/strategies/oi_ml_ce_seller.py` | Done, opt-in order routing buys the hedge first, rolls back on short rejection, blocks entries after cutoff, and exits residual spreads by time/EOD stops |

## Inputs Required Before a Candidate Can Pass

The current runtime path needs these fields per option quote:

| Field | Source | Why it matters |
|---|---|---|
| `underlying`, `expiry`, `strike`, `option_type` | Angel scrip master | Contract identity and candidate filtering |
| `trading_symbol`, `exchange`, `symbol_token` | Angel scrip master | Auditability and eventual order-intent construction |
| `oi` | Angel FULL quote | OI wall, PCR, max pain, concentration features |
| `volume` | Angel FULL quote | Quote completeness and future liquidity features |
| `iv` | Angel FULL quote when supplied; otherwise recent exact-contract `nse_web` validation row at read time | Volatility features and later sigma filters |
| `bid`, `ask`, `ltp` | Angel FULL quote | Premium, bid/ask quality, labels, stops |
| `source_ts` | Angel quote payload when available | Staleness detection |
| `underlying_ltp` | NSE NIFTY context LTP fallback, or option payload if supplied | OTM filter, distance features, spot stops |
| `vix` | NSE India VIX context LTP fallback, or option payload if supplied | Option-sell guard and naked/spread gating |

Missing hard fields are persisted in `quality_flags`; live entry gates must reject
rows with hard quality flags. Missing Angel IV is an optional field. When the
repository enriches IV from stored NSE validation rows, it does not update the
Angel row; the returned in-memory quote is tagged with
`iv_enrichment_mode=read_time`, `iv_enriched_from_provider=nse_web`, and the
reference snapshot timestamp.

## Resolved Gaps

### Provider calendar mismatch

The old default assumed a Thursday weekly NIFTY expiry. On 2026-05-18, Angel's
scrip master listed NIFTY option expiries starting at `2026-05-19`, and had zero
NIFTY option rows for `2026-05-21`. The sidecar now resolves the next listed
option expiry from the provider calendar at startup. Explicit configured expiries
are rejected if they are not listed.

### Missing spot and VIX inputs

The first provider version relied on option quote payloads for `underlying_ltp`
and did not populate `vix`. The adapter now fetches NSE context rows for NIFTY
spot and India VIX and stamps those values onto option quotes when the option
payload does not provide them.

### Log/cache permissions

The sidecar runs as container user `appuser`; `/opt/phoenix/logs/oi-ml-shadow`
must be writable by that user. Host ownership was corrected to allow scrip-master
caching.

### Ingestion visibility

The main dashboard health summary exposes read-only sidecar evidence under
`oi_ml_shadow_ingestion`:

```bash
docker exec phoenix-oci-backend \
  curl -sS http://localhost:8080/health/summary
```

The live backend observes the separate sidecar with
`OI_ML_SHADOW_HEALTH_ENABLED=true`. This is a dashboard-only health flag: it
does not start the shadow runner in the backend and does not create an order
route. The payload includes today's `option_chain_1m` row count, latest
snapshot/source/ingest timestamps, validation report count, shadow intent count,
and a dry-run invariant (`live_order_path_enabled=false`). If the sidecar is
expected after the snapshot window starts and no option-chain rows are present,
dashboard status is degraded with reason `oi_ml_shadow_ingestion_degraded`; the
alert rule `oi_ml_shadow_ingestion_degraded` fires with a sanitized row count.

Repeated provider/login timeouts are logged as compact
`oi_ml_shadow_ingestion_degraded` warnings with a consecutive-failure count.
Detailed stack traces are debug-level only on the first failure and every tenth
failure, so the retry signal stays visible without flooding operator logs.
If the health payload reports `option_chain_rows_missing` during the snapshot
window, first check sidecar logs for `ANGEL_LOGIN_RETRY`, provider timeout, and
proxy/session reuse messages. The health payload includes an `operator_hint`
field for this path and always carries the dry-run invariant
`live_order_path_enabled=false`; do not remediate sidecar ingestion by enabling
any live order path.

The sidecar container has its own Docker healthcheck:

```bash
python -m app.strategies.oi_ml.shadow_health
```

The command prints sanitized JSON and exits non-zero when the expected sidecar
evidence is degraded or unavailable. Freshness is enforced only during the
snapshot window; after the window closes, today's completed snapshot remains
healthy instead of being marked stale overnight.

## Remaining Promotion Gate

Do not promote this strategy beyond shadow until the Phase-0 approval report in
[OI/ML Option-Chain Data Source Approval](oi_ml_data_source_approval.md) passes
and a market-window snapshot proves real broker FULL quote completeness for the
hard fields and proves that IV is available either directly from Angel or
through fresh exact-contract NSE validation rows. After this data gate passes, use
[OI/ML CE Seller Rollout and Rollback](oi_ml_ce_seller_rollout.md) for the
paper, shadow, Live A, Live B, and rollback checklist. The 2026-05-18
off-market smoke proved connectivity and
storage, but all off-market rows were flagged because source timestamps were
stale.

Required proof from `public.option_chain_1m` for the active listed NIFTY expiry:

```sql
SELECT
  count(*) FILTER (WHERE provider = 'angel') AS angel_rows,
  count(oi) FILTER (WHERE provider = 'angel') AS oi_rows,
  count(volume) FILTER (WHERE provider = 'angel') AS volume_rows,
  count(iv) FILTER (WHERE provider = 'angel') AS direct_angel_iv_rows,
  count(bid) FILTER (WHERE provider = 'angel') AS bid_rows,
  count(ask) FILTER (WHERE provider = 'angel') AS ask_rows,
  count(ltp) FILTER (WHERE provider = 'angel') AS ltp_rows,
  count(underlying_ltp) FILTER (WHERE provider = 'angel') AS spot_rows,
  count(vix) FILTER (WHERE provider = 'angel') AS vix_rows,
  count(iv) FILTER (WHERE provider = 'nse_web') AS nse_iv_rows,
  count(*) FILTER (
    WHERE provider = 'angel' AND quality_flags <> '{}'::jsonb
  ) AS flagged_angel_rows
FROM public.option_chain_1m
WHERE underlying = 'NIFTY'
  AND expiry = DATE '2026-05-26'
  AND snapshot_ts >= now() - interval '1 day';
```

Expected result before promotion:

- `angel_rows > 0`.
- `oi_rows`, `volume_rows`, `bid_rows`, `ask_rows`, `ltp_rows`, `spot_rows`,
  and `vix_rows` should be close to `angel_rows`.
- `direct_angel_iv_rows` may be low if Angel omits IV. In that case, confirm
  `nse_iv_rows` exists for matching fresh rows and repository-returned quotes
  carry `iv_enrichment_mode=read_time`. NSE live-derivatives fallback rows do
  not contain IV and do not satisfy this promotion proof.
- Any `flagged_angel_rows` must be explained and must not include required candidate
  strikes.

Earlier sidecar login timeouts were fixed by forwarding the same broker proxy
environment used by the live backend and by reusing a read-only Angel quote
session. The field-completeness gate is still open until a live market-window
snapshot shows fresh source timestamps and usable IV, either direct from Angel
or enriched from stored NSE validation rows that include IV.

## NSE Cross-Validation

NSE's public option-chain data can be used as an operator-triggered validation
source. It must not be used as a live trading feed or order-routing dependency.
The preferred classic NSE option-chain JSON can validate OI, volume, IV, bid,
ask, and LTP. On the OCI VM, that endpoint has been observed returning HTTP 200
with an empty JSON object; for NIFTY only, the adapter then falls back to NSE's
live-derivatives endpoint and validates the fields that endpoint publishes:
OI, volume, and LTP.

Automatic continuous validation is available inside the OI/ML sidecar and the
sidecar compose defaults `OI_ML_ENABLE_NSE_VALIDATION=true`. When enabled, each
captured Angel snapshot triggers validation. The snapshot/validation window is
separate from the entry window: data capture runs from `09:15` to `15:30` IST,
while shadow trade decisions remain restricted to `09:45` to `14:30` IST.

- an NSE web option-chain pull for the same underlying/expiry, with NIFTY
  fallback to `liveEquity-derivatives` when the classic payload is empty;
- optional NSE quote persistence as `provider='nse_web'`;
- an Angel-vs-NSE comparison for comparable fields;
- one compact container-log observation per snapshot, with warnings for
  mismatches, provider-only contracts, missing IV, or fetch errors;
- a full JSON report in `public.option_chain_validation_reports` for EOD review.

When the live-derivatives fallback is used, reference quotes are tagged with
`nse_source=live_equity_derivatives`. The realtime validator records
`reference_sources=["live_equity_derivatives"]` and
`skipped_missing_reference_fields=["ask","bid","iv"]` in report metadata. The
fallback normalizes NSE open interest into the broker-unit convention used by
stored Angel rows. If NSE still returns no comparable rows, the validator records
`ERROR/ERROR` with `nse_reference_quotes_empty` instead of a misleading
`MISMATCH/WARN` zero-reference report.

The strategy repository may use stored `nse_web` rows only to fill missing IV in
the returned in-memory Angel quote when those rows actually contain IV. It
requires the same underlying, expiry, strike, and option type, and the reference
snapshot must be no more than 120 seconds older than the Angel snapshot. This is
enrichment for shadow analytics, not an order-routing dependency, and raw stored
provider rows remain separate. The live-derivatives fallback does not contain
IV, so it is validation evidence for OI/volume/LTP only.

Sidecar env:

```text
OI_ML_ENABLE_NSE_VALIDATION=true
OI_ML_SHADOW_SNAPSHOT_START_TIME=09:15
OI_ML_SHADOW_SNAPSHOT_END_TIME=15:30
OI_ML_NSE_VALIDATION_STORE_QUOTES=true
OI_ML_NSE_VALIDATION_LOG_ALL=true
OI_ML_NSE_VALIDATION_FAIL_ON_ERROR=false
OI_ML_NSE_VALIDATION_TIMEOUT_SECONDS=10
```

`FAIL_ON_ERROR=false` is deliberate for shadow mode: validation failures are
logged and persisted without stopping Angel snapshot ingestion. Do not wire this
table into live order routing.

Compare the latest stored Angel snapshot against a saved NSE payload:

```bash
python scripts/data/validate_nse_vs_angel_option_chain.py \
  --expiry 2026-05-19 \
  --decision-ts 2026-05-18T10:00:00+00:00 \
  --nse-json-input /path/to/nse_option_chain.json
```

Fetch NSE directly, compare with the latest Angel snapshot from the previous
15 minutes, and store normalized NSE rows as `provider='nse_web'` for audit:

```bash
python scripts/data/validate_nse_vs_angel_option_chain.py \
  --expiry 2026-05-19 \
  --lookback-minutes 15 \
  --store-nse \
  --output-json /tmp/nse_vs_angel_validation.json
```

The command prints a JSON report with:

- matched contract count;
- Angel-only and NSE-only contracts;
- per-field differences outside configured tolerances;
- missing IV counts by provider;
- `validation_only=true` metadata.

Default tolerances are intentionally narrow: exact OI, volume within `250` or
`5%`, price fields within `0.10` or `1%`, and IV within `0.50` or `5%`. Use
the `--*-tolerance` flags only for manual investigation; do not hide systematic
provider drift by widening them permanently.

End-of-day summary:

```bash
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix -c "
SELECT
  status,
  severity,
  count(*) AS reports,
  sum(mismatch_count) AS mismatches,
  sum(primary_only_count) AS angel_only,
  sum(reference_only_count) AS nse_only,
  sum(missing_primary_iv) AS missing_angel_iv,
  sum(missing_reference_iv) AS missing_nse_iv
FROM public.option_chain_validation_reports
WHERE validation_ts::date = CURRENT_DATE
GROUP BY status, severity
ORDER BY status, severity;"
```

Recent abnormal observations:

```bash
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix -c "
SELECT validation_ts, snapshot_ts, underlying, expiry, status, severity,
       compared_contracts, mismatch_count, primary_only_count,
       reference_only_count, missing_primary_iv, missing_reference_iv
FROM public.option_chain_validation_reports
WHERE validation_ts::date = CURRENT_DATE
  AND status <> 'OK'
ORDER BY validation_ts DESC
LIMIT 50;"
```

## Safe Operations

Inspect sidecar status:

```bash
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" \
  | grep -E "phoenix-oci|phoenix-oi-ml"
docker logs phoenix-oi-ml-shadow --tail 80
```

Restart the sidecar with smoke constants:

```bash
cd /opt/phoenix
IMAGE_TAG=oi-ml-shadow-bd999cd \
OI_ML_SHADOW_SCORER=constant \
OI_ML_SHADOW_CONSTANT_PROBABILITY=0.64 \
OI_ML_SHADOW_CONSTANT_MAE_PREMIUM=40 \
docker compose -f /opt/phoenix/oi-ml-shadow.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d oi-ml-shadow
```

Do not add blank `ANGEL_HTTPS_PROXY` or `HTTPS_PROXY` entries under the sidecar
`environment` block. Those keys come from `/opt/phoenix/phoenix-deploy.env`;
blank compose interpolation overrides the env file and causes Angel login
timeouts.

Validate tables:

```bash
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix \
  -c "\dt public.option_chain_1m" \
  -c "\dt public.oi_ml_shadow_order_intents" \
  -c "\dt public.option_chain_validation_reports"
```

Check shadow intent count:

```bash
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix -tAc \
  "SELECT count(1) FROM public.oi_ml_shadow_order_intents"
```

## LightGBM Mode

`OI_ML_SHADOW_SCORER=lightgbm` is supported, but it requires real artifacts:

```text
OI_ML_SHADOW_LIGHTGBM_CLASSIFIER_PATH
OI_ML_SHADOW_LIGHTGBM_FEATURE_NAMES_PATH
OI_ML_SHADOW_LIGHTGBM_MAE_MODEL_PATH
OI_ML_SHADOW_LIGHTGBM_DEFAULT_MAE_PREMIUM
```

The sidecar compose mounts `/opt/phoenix/oi-ml-models` at `/app/models:ro`.
Do not enable LightGBM mode until trained artifacts have passed walk-forward and
market-session snapshot completeness checks.

## Live Stack Restart Note

The live OCI containers may be gracefully stopped by the scheduled midnight IST
shutdown path. If backend/nginx need to be started manually, use the documented
live compose command with `--no-deps` so the migrator dependency is not started
unintentionally:

```bash
cd /opt/phoenix/app
CONTROL_PLANE_PG_PASSWORD_HOST="$(sudo cat /run/secrets/control_plane_pg_password)" \
docker compose -f docker-compose.oci-live.yml \
  -f /opt/phoenix/phoenix-override.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d --no-deps backend nginx
```
