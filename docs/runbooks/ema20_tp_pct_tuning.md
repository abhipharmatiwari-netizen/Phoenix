# EMA20 `tp_pct` tuning from PHX#186 telemetry

**Status:** Pending data accumulation
**Earliest viable run:** ~early July 2026 (when ~30 trading days of LIVE attribution data exist)
**Tracks:** [#185](https://github.com/abhipharmatiwari-netizen/Phoenix/issues/185)

## Why this runbook exists

The PHX#185 backtest sweep (2026-05-06) was structurally blocked: the replay's option-pricing proxy lacks theta decay, so no `tp_pct ∈ {0.15, 0.20, 0.25, 0.30, 0.35, 0.45, 0.50}` ever fires in replay. See [docs/parameters.md](../parameters.md) for the sweep result and [issue #185](https://github.com/abhipharmatiwari-netizen/Phoenix/issues/185#issuecomment-4386330642) for full context.

The path forward is to wait for the PHX#186 exit-attribution telemetry — shipped in the same release as #182/#183/#184 — to accumulate from real LIVE fills. Each EMA20 exit writes one record to `${APP_LOG_DIR:-/app/logs}/exit_attribution.jsonl` with peak profit, final profit, regime at entry/exit, and exit reason. After ~30 trading days the dataset is large enough to drive per-regime `tp_pct` recommendations from real intra-day option behaviour.

## Pre-requisites — verify before running tuning

These checks should pass before the analysis is meaningful. Run them in order; **stop if any fail**.

### 1. Confirm new code is actually deployed

The PHX#182/#183/#184/#186 changes were committed `ee3ff83` on 2026-05-06. The container image must be rebuilt and redeployed for the attribution code to run.

```bash
# On OCI VM (over bastion):
docker exec phoenix-oci-backend grep -c "_emit_exit_attribution" /app/app/strategies/ema20_strategy.py
# Expected: 1 (function defined)
# If 0: image is pre-ee3ff83 — NEEDS REBUILD AND REDEPLOY before any data accumulates.
```

### 2. Confirm telemetry file exists and is growing

```bash
docker exec phoenix-oci-backend ls -la /app/logs/exit_attribution.jsonl
docker exec phoenix-oci-backend wc -l /app/logs/exit_attribution.jsonl
```

If the file doesn't exist, the new code isn't running OR no exits have happened yet. Cross-check with backend logs for the `exit_attribution` log event.

### 3. Confirm persistence across restarts

The JSONL file must survive container recreation. Verify the mount:

```bash
docker inspect phoenix-oci-backend --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
# /app/logs should resolve to a host path under /opt/phoenix/logs or similar.
```

If `/app/logs` is NOT mounted to a host path, fix [docker-compose.oci-live.yml](../../docker-compose.oci-live.yml) to add a volume mount before tuning — or you'll lose all telemetry on the next image redeploy.

### 4. Sample size threshold

Minimum: **30 final-exit trades per (underlying, regime) cell** for Sharpe to be marginally meaningful. With ~1 trade/day per underlying historical rate, 30 days × 3 underlyings ÷ ~4 regimes = ~22 trades per cell on average. Some cells (e.g., HIGH_VOL) may take longer to fill.

Quick check:
```bash
docker exec phoenix-oci-backend python -c "
import json, collections
counts = collections.Counter()
with open('/app/logs/exit_attribution.jsonl') as fh:
    for line in fh:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get('final'):
            counts[(r.get('underlying',''), r.get('regime_at_entry',''))] += 1
for k, n in sorted(counts.items()):
    print(f'{k}: {n}')
"
```

## Tuning procedure

When the pre-requisites pass:

### 1. Pull the JSONL down

```bash
# On OCI VM
docker cp phoenix-oci-backend:/app/logs/exit_attribution.jsonl /tmp/exit_attribution.jsonl
# From laptop
scp -i <key> -o ProxyCommand=... opc@10.0.2.83:/tmp/exit_attribution.jsonl ./
```

### 2. Run the analysis

From the project root:
```bash
.backtest_venv/Scripts/python scripts/ops/analyze_exit_attribution.py exit_attribution.jsonl
```

Output: per-underlying and per-(underlying, regime) ranking of `tp_pct` candidates by Sharpe, mean profit, win rate, and capture rate.

### 3. Apply judgement before changing config

The analyser uses a counterfactual ("if `tp_pct` had fired first, P&L = `tp_pct × entry`"). It assumes the trade visited `peak_favorable_pct` *before* the final exit (monotone path). For most trades this holds; for U-shapes it overstates capture.

**Don't blindly take the rank-1 value.** Look for:
- Stable ranking across regimes (a value that's top-3 in every regime is a safer pick than rank-1 in TRENDING but rank-7 in CHOPPY)
- Capture rate ≥ 30% (otherwise the rank is dominated by leftover EOD trades, not actual TP captures)
- Sharpe materially > current default's Sharpe

### 4. Update `strategy_env.yaml`

Edit per-regime `tp_pct` in [app/config/strategy_env.yaml](../../app/config/strategy_env.yaml). Per #185:
- Update TRENDING / NORMAL / CHOPPY / HIGH_VOL profiles for each underlying
- Document chosen values + the analyser's evidence in [docs/parameters.md](../parameters.md)
- Re-run `pytest tests/strategies/test_ema20_*` to confirm nothing breaks

### 5. DEMO smoke before LIVE

Per #185 acceptance criterion: deploy to DEMO mode for one full trading day, watch `exit_attribution.jsonl` for the new `tp_pct` actually firing in real fills (capture rate should approach the analyser's prediction). Only then promote to LIVE.

### 6. Close #185

PR comment + close with link to:
- The JSONL snapshot used (anonymised if needed)
- Analyser output table
- DEMO smoke test results
- The strategy_env.yaml diff

## Schema v2 enhancement (optional, future)

Current `exit_attribution.jsonl` schema (v1) records peak profit pct but NOT the timestamp at peak. A peak-after-final-exit pattern is invisible — the counterfactual assumes monotone path. Adding `peak_ts` to the schema (v2) would let the analyser correctly reject overstated captures.

If the v1 dataset shows wide variance in tuning recommendations, ship v2 first then re-tune.
