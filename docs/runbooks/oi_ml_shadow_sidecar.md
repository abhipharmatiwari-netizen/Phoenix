# OI/ML Shadow Sidecar Runbook

Status: current progress record for the OI/ML CE seller shadow sidecar as of
2026-05-18.

This sidecar is a dry-run research and validation process. It must not place,
modify, cancel, or exit live orders. It runs beside the live OCI stack, publishes
no host ports, writes normalized option-chain snapshots and shadow order intents
to Postgres, and keeps `OI_ML_SHADOW_ALLOW_NAKED=false`.

## Current State

| Area | State |
|---|---|
| Branch | `oi-ml-shadow-sidecar` |
| Latest deployed sidecar commit | `9e91b77` |
| OCI checkout | `/opt/phoenix/oi-ml-shadow-src` |
| Compose file | `/opt/phoenix/oi-ml-shadow.yml` |
| Running image | `phoenix-oi-ml-shadow:oi-ml-shadow-9e91b77` |
| Container | `phoenix-oi-ml-shadow` |
| Database tables | `public.option_chain_1m`, `public.oi_ml_shadow_order_intents` |
| Default scorer | `missing` in compose; deployed smoke override uses `constant` |
| Smoke scorer currently used | `OI_ML_SHADOW_SCORER=constant`, probability `0.64`, MAE premium `40` |
| Broker proxy/session | Sidecar now forwards backend broker proxy env and reuses the Angel quote session during the snapshotter session |
| LightGBM support | Implemented with `lightgbm==4.6.0`; artifact paths must be explicit |
| Live order routing | Not used by the sidecar |

Recent validation:

- Local focused OI/ML suite: `159 passed`.
- Sidecar startup resolved provider-listed NIFTY expiry from Angel scrip master:
  `calendar_default=2026-05-21 listed=2026-05-19`.
- Live backend and nginx were healthy after restart: `/readyz=200`, `/health=200`.
- Off-market smoke on 2026-05-18 21:11 IST proved Angel login through proxy and
  FULL/LTP quote calls: `220` NIFTY rows fetched/stored, `0` shadow intents
  because scorer was fail-closed for the smoke run.

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
| Strategy scaffold | `app/strategies/oi_ml_ce_seller.py` | Done, fail-closed and disabled by default |

## Inputs Required Before a Candidate Can Pass

The current runtime path needs these fields per option quote:

| Field | Source | Why it matters |
|---|---|---|
| `underlying`, `expiry`, `strike`, `option_type` | Angel scrip master | Contract identity and candidate filtering |
| `trading_symbol`, `exchange`, `symbol_token` | Angel scrip master | Auditability and eventual order-intent construction |
| `oi` | Angel FULL quote | OI wall, PCR, max pain, concentration features |
| `volume` | Angel FULL quote | Quote completeness and future liquidity features |
| `iv` | Angel FULL quote | Volatility features and later sigma filters |
| `bid`, `ask`, `ltp` | Angel FULL quote | Premium, bid/ask quality, labels, stops |
| `source_ts` | Angel quote payload when available | Staleness detection |
| `underlying_ltp` | NSE NIFTY context LTP fallback, or option payload if supplied | OTM filter, distance features, spot stops |
| `vix` | NSE India VIX context LTP fallback, or option payload if supplied | Option-sell guard and naked/spread gating |

Missing hard fields are persisted in `quality_flags`; live entry gates must reject
rows with hard quality flags.

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

## Remaining Promotion Gate

Do not promote this strategy beyond shadow until a market-window snapshot proves
real broker FULL quote completeness. The 2026-05-18 off-market smoke proved
connectivity and storage, but all off-market rows were flagged because `iv` was
missing and source timestamps were stale.

Required proof from `public.option_chain_1m` for the active listed NIFTY expiry:

```sql
SELECT
  count(*) AS rows,
  count(oi) AS oi_rows,
  count(volume) AS volume_rows,
  count(iv) AS iv_rows,
  count(bid) AS bid_rows,
  count(ask) AS ask_rows,
  count(ltp) AS ltp_rows,
  count(underlying_ltp) AS spot_rows,
  count(vix) AS vix_rows,
  count(*) FILTER (WHERE quality_flags <> '{}'::jsonb) AS flagged_rows
FROM public.option_chain_1m
WHERE underlying = 'NIFTY'
  AND expiry = DATE '2026-05-19'
  AND snapshot_ts >= now() - interval '1 day';
```

Expected result before promotion:

- `rows > 0`.
- `oi_rows`, `volume_rows`, `iv_rows`, `bid_rows`, `ask_rows`, `ltp_rows`,
  `spot_rows`, and `vix_rows` should be close to `rows`.
- Any `flagged_rows` must be explained and must not include required candidate
  strikes.

Earlier sidecar login timeouts were fixed by forwarding the same broker proxy
environment used by the live backend and by reusing a read-only Angel quote
session. The field-completeness gate is still open until a live market-window
snapshot shows usable `iv` and fresh source timestamps.

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
IMAGE_TAG=oi-ml-shadow-9e91b77 \
OI_ML_SHADOW_SCORER=constant \
OI_ML_SHADOW_CONSTANT_PROBABILITY=0.64 \
OI_ML_SHADOW_CONSTANT_MAE_PREMIUM=40 \
docker compose -f /opt/phoenix/oi-ml-shadow.yml \
  --env-file /opt/phoenix/phoenix-deploy.env \
  up -d oi-ml-shadow
```

Validate tables:

```bash
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix \
  -c "\dt public.option_chain_1m" \
  -c "\dt public.oi_ml_shadow_order_intents"
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
