# Optimized Strategy Parameters

> **Status:** Research/reference, not authoritative LIVE configuration.
> Current runnable values come from `app/config/strategy_env.yaml`, database-backed
> strategy config, and runtime overrides. Do not apply parameter changes from this
> file directly to LIVE without release evidence and operator sign-off.
> As of 2026-06-03, LIVE routing is EMA20-only: `ema20_strategy` is the
> only enabled live strategy, enabled instruments allow only `ema20_strategy`,
> and `AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING=1`. Non-EMA sections below
> are historical research references only.

**Backtest period:** 2026-02-23 to 2026-03-20 (19 trading days)
**Data source:** `indicator_bars` table (PostgreSQL)
**Generated:** 2026-03-21

---

## 1. exclusive_nifty_ce_buy

**Instrument:** NIFTY_IDX | **Timeframe:** 30s bars | **Direction:** Long ATM CE
**Data:** 13,798 bars with full indicators across 19 trading days

### Backtest Results Summary

| Config | Trades | Wins | Win% | Total PnL (pts) | PF | Max DD |
|--------|--------|------|------|-----------------|------|--------|
| **CURRENT** | 19 | 14 | 73.7% | 101.5 | 2.20 | 44.1 |
| **ADX16_DI2 (BEST)** | 19 | 16 | 84.2% | 160.2 | 5.09 | 23.1 |
| OPT_B | 19 | 14 | 73.7% | 119.6 | 2.70 | 21.4 |
| TIGHT_SL | 19 | 14 | 73.7% | 116.9 | 2.68 | 36.1 |
| SAFE | 19 | 15 | 78.9% | 107.9 | 2.37 | 39.6 |
| MACD_LOW | 19 | 14 | 73.7% | 105.8 | 2.25 | 44.1 |

### Optimized Parameters

```yaml
# exclusive_nifty_ce_buy — strategy_env.yaml
- name: "exclusive_nifty_ce_buy"
  enabled: false  # disabled in LIVE as of 2026-06-03 EMA20-only routing
  instruments:
    - "NIFTY_IDX"
  params:
    timeframe_seconds: 30
    # --- Entry Filters (optimized) ---
    rsi_min: 58              # no change — already well-calibrated
    rsi_max: 72              # no change
    macd_hist_min: 0.30      # no change — 43.8% bars pass, good selectivity
    min_adx: 16.0            # CHANGED from 20.0 — relaxed, catches more valid entries
    min_di_spread: 2.0       # CHANGED from 5.0 — relaxed, +34.8% -> 52.1% pass rate
    vol_quantile: 0.45       # no change
    ema_atr_buffer: 0.05     # no change — 48.7% pass rate, good balance
    strict_trend: false      # no change
    allow_near_macd: true
    macd_near: 0.40
    # --- Trade Management (no change) ---
    sl_atr: 2.2
    tp_atr: 2.5
    ema_fail_bars: 3
    ema_fail_buffer_atr: 0.10
    trail_active_atr: 0.8
    trail_cushion_atr: 0.16
    cooldown_bars: 2
    max_trades_per_day: 1
    # --- Session ---
    session_start: "10:15"
    last_entry_time: "14:45"
    squareoff_time: "15:15"
    late_start: "14:45"
    late_tp_cap_atr: 2.6
    late_trail_cushion: 0.08
    late_trail_active_atr: 0.6
    lots_per_trade: 1
    product_type: "INTRADAY"
```

### Key Findings
- **ADX and DI_spread were the biggest drag.** Relaxing ADX from 20->16 and DI_spread from 5->2 added 2 more winning trades and eliminated 58.7 pts of unnecessary drawdown.
- **Profit factor jumped from 2.20 to 5.09** — the current filters were rejecting valid bullish entries in moderate-trend environments.
- **SL/TP ATR multiples are well-tuned.** Tighter SL (1.8x) slightly improved PnL but increased DD. Current 2.2x/2.5x is optimal risk-adjusted.
- **RSI [58-72] and MACD_hist > 0.30 are well-calibrated.** Filter pass rates (21.2% RSI, 43.8% MACD) provide good signal selectivity.

### Environment Variables
```bash
# No new env vars needed — parameters are in strategy_env.yaml
# Dynamic policy profiles should also relax ADX/DI:
#   TRENDING: min_adx=16.0, min_di_spread=2.0
#   NORMAL:   min_adx=18.0, min_di_spread=3.0
```

---

## 2. ema20_strategy

**Instruments:** NIFTY_IDX, BANKNIFTY_IDX, NG_FUT | **Timeframe:** 5m (300s) bars | **Direction:** Short CE (bearish)
**Data:** 1,174 bars (NIFTY/BANKNIFTY), 2,802 bars (NG_FUT) across 19 trading days

### Backtest Results Summary

#### NIFTY_IDX
| Config | Trades | Wins | Win% | Total PnL% | PF |
|--------|--------|------|------|------------|------|
| **CURRENT (ATR>45)** | 6 | 2 | 33.3% | -0.45% | 0.85 |
| **ATR_20 (BEST)** | 18 | 8 | 44.4% | -0.20% | 0.96 |
| ATR_30 | 12 | 4 | 33.3% | -0.51% | 0.87 |
| SAFE (ADX>22, DI>5) | 9 | 4 | 44.4% | -0.43% | 0.87 |

#### BANKNIFTY_IDX
| Config | Trades | Wins | Win% | Total PnL% | PF |
|--------|--------|------|------|------------|------|
| **CURRENT** | 19 | 8 | 42.1% | +0.28% | 1.05 |
| **NO_RSI (BEST)** | 19 | 8 | 42.1% | +0.41% | 1.08 |
| ADX_ON | 17 | 7 | 41.2% | -2.12% | 0.66 |

#### NG_FUT (EMA-8, MCX session 09:30–23:30)
| Config | Trades | Wins | Win% | Total PnL% | PF |
|--------|--------|------|------|------------|------|
| **E8_CURRENT (ATR>0.9)** | 7 | 5 | 71.4% | +9.19% | 1.74 |
| **E8_ATR12 (BEST)** | 7 | 6 | 85.7% | +14.40% | 2.55 |
| E8_ATR05 | 7 | 4 | 57.1% | +11.01% | 1.92 |
| E8_ADX_ON (ADX>22) | 7 | 5 | 71.4% | +7.66% | 1.62 |
| SAFE (ATR>1.2, ADX>24) | 5 | 4 | 80.0% | +14.60% | 411.29 |

### Optimized Parameters

```yaml
# ema20_strategy (NIFTY_IDX) — strategy_env.yaml
- name: ema20_strategy
  enabled: true
  instruments:
    - "NIFTY_IDX"
  params:
    ema_period: 20
    timeframe_seconds: 300
    min_atr: 25.0            # CHANGED from 45.0 — 45 was too restrictive (only 6/19 days)
    require_rsi_falling: true
    use_adx_filter: false    # no change — ADX filter hurts on NIFTY
    adx_period: 14
    min_adx: 18.0
    require_bearish_di: true
    min_di_spread: 0.0
    first_entry_time: "9:30"
    square_off_time: "15:00"
    sl_pct: 0.20
    tp_pct: 0.30
    trail_buffer_pct: 0.05
    trail_trigger_pct: 0.10

# ema20_strategy (BANKNIFTY_IDX) — strategy_env.yaml
- name: ema20_strategy
  enabled: true
  instruments:
    - "BANKNIFTY_IDX"
  params:
    ema_period: 20
    timeframe_seconds: 300
    min_atr: 0               # CHANGED — keep at 0 (BANKNIFTY ATR always sufficient)
    require_rsi_falling: false  # CHANGED from true — improves PnL by +0.13%
    use_adx_filter: false    # no change — ADX filter causes -2.12% loss
    adx_period: 14
    min_adx: 18.0
    require_bearish_di: true
    min_di_spread: 0.0
    square_off_time: "15:00"
    sl_pct: 0.20
    tp_pct: 0.30
    trail_buffer_pct: 0.10
    trail_trigger_pct: 0.15

# ema20_strategy (NG_FUT) — strategy_env.yaml
- name: ema20_strategy
  enabled: true
  instruments:
    - "NG_FUT"
  params:
    ema_period: 8              # no change — EMA-8 outperforms EMA-20 on NG
    min_atr: 1.2               # CHANGED from 0.9 — higher ATR filter gives 85.7% win rate
    require_rsi_falling: false  # no change
    timeframe_seconds: 300
    signal_timeframe: 300
    use_adx_filter: false      # no change — ADX filter slightly hurts
    adx_period: 14
    min_adx: 22.0
    require_bearish_di: true
    min_di_spread: 2.0
    first_entry_time: "09:30"
    square_off_time: "23:30"
    sl_pct: 0.20
    tp_pct: 0.30
    trail_buffer_pct: 0.10
    trail_trigger_pct: 0.20
```

### Key Findings
- **All EMA20 exits are EOD squareoffs** — SL/TP percentages (20%/30%) never triggered on option price within a single day. This means the strategy is acting as a daily directional play, not a scalp.
- **NIFTY min_atr=45.0 is far too restrictive.** Median 5m ATR is ~39.7 — the filter blocked 13 of 19 trading days. Lowering to 25.0 allows 18 entries with improved results.
- **BANKNIFTY is profitable with no RSI filter.** RSI_falling requirement removes a few profitable entries. Without it, PF improves to 1.08.
- **NG_FUT is the strongest EMA20 performer.** EMA-8 on NG yields +14.40% over 19 days (PF=2.55). Raising min_atr from 0.9 to 1.2 filters out 2 weak-ATR days and boosts win rate from 71.4% to 85.7%. The SAFE config (ATR>1.2, ADX>24, RSI_falling) yields PF=411 on 5 trades but is overfitted to this sample.
- **ADX filter is harmful** on NIFTY and BANKNIFTY but neutral on NG_FUT — NG already has strong directional moves.
- **SL/TP re-calibration needed:** Current pct-based SL/TP on option premiums don't trigger intraday. PHX#182/#183/#184 (below) add partial booking, give-back, and time-decay tightening to address this.

### Profit-Booking Enhancements (PHX#182, #183, #184, #186)

These tunables ship **disabled by default** — current behaviour is preserved unless explicitly enabled per regime/instrument in `strategy_env.yaml` or via env vars.

#### TP1 partial booking (PHX#182)
Books a fraction of the position at a first target, trails the residual on existing TP/SL/TRAIL logic. Closes at least one lot less than the full position (always leaves a runner).

```yaml
params:
  tp1_pct: 0.20         # fire TP1 when premium drops 20%
  tp1_qty_pct: 0.5      # close 50% of original lots
```

Position state on partial fill: `qty` decrements, `tp1_filled=true`, position kept open. Order tag: `EMA20_EXIT_TP1`. Subsequent triggers (TP/SL/TRAIL/EOD) close the residual.

#### Give-back guardrail (PHX#183)
Tracks peak favourable move; exits if profit retraces from peak by configured fraction. Useful when trailing is loose or disabled (e.g., CHOPPY regime).

```yaml
params:
  giveback_pct: 0.5         # exit if profit drops to 50% of peak
  giveback_arm_pct: 0.10    # only arm after 10% favourable move
```

Order tag: `EMA20_EXIT_GIVEBACK`. Evaluation order: SL > GIVEBACK > TRAIL > TP1 > TP. SL always wins.

#### Time-decay accelerator (PHX#184)
Tightens `tp_pct` and `trail_buffer_pct` once minutes-to-square-off drops below a threshold. Recompute is one-shot per position (`decay_window_applied` flag).

```yaml
params:
  decay_tighten_minutes_before_eod: 90    # tighten in last 90 minutes
  decay_tp_multiplier: 0.7                # tp_pct × 0.7 in window
  decay_trail_buffer_multiplier: 0.5      # trail × 0.5 in window
```

Multipliers > 1.0 are ignored (never loosen `tp_price` mid-trade).

#### Exit attribution telemetry (PHX#186)
Every exit emits a structured log line and writes a JSONL record to `${APP_LOG_DIR:-/app/logs}/exit_attribution.jsonl`:

```json
{"v":1,"schema":"ema20.exit_attribution","reason":"TP1","final":false,
 "entry_price":100.0,"exit_price":80.0,"peak_favorable_pct":0.20,
 "final_profit_pct":0.20,"held_seconds":3450,"trail_was_active":false,
 "tp1_filled":true,"decay_window_applied":false,"exit_lots":2,
 "original_qty":4,"remaining_qty":2,"regime_at_entry":"TRENDING",
 "regime_at_exit":"TRENDING","policy_id":"ema20_nifty_v1"}
```

Use this to A/B test whether partial booking improves realised P&L vs. trailing alone. Schema-versioned (`v: 1`) so future fields don't break downstream tooling.

### Environment Variables
```bash
# NIFTY EMA20
NIFTY_EMA20_MIN_ATR=25.0

# BANKNIFTY EMA20
BANKNIFTY_EMA20_REQUIRE_RSI_FALLING=false

# NG EMA20
NG_EMA20_MIN_ATR=1.2
```

All profit-booking params (PHX#182/#183/#184) are also accessible via the `_live_param_*` resolver, so dynamic-policy regime profiles can override them in `strategy_env.yaml`.

### PHX#185 Sweep Result — `tp_pct` is not tunable in current replay (2026-05-06)

A 504-combo `tp_pct` sweep was run against the local-DB `indicator_bars` (39 trading days, 2026-02-23 to 2026-04-30) on all three underlyings. **Across all 42 base-param groups (15 NIFTY + 15 BANKNIFTY + 12 NG_FUT), every `tp_pct ∈ {0.15, 0.20, 0.25, 0.30, 0.35, 0.45, 0.50}` produced identical net P&L.** The optimizer's per-underlying conclusion was unchanged.

| Underlying | Trades | Net P&L (any tp_pct) | Discriminating groups |
|---|---|---|---|
| NIFTY_IDX | 33 | -1740.02 | 0 / 15 |
| BANKNIFTY_IDX | 37 | -1948.47 | 0 / 15 |
| NG_FUT | varied | -691.16 | 0 / 12 |

**Root cause:** Replay's option-pricing model is a deterministic underlying-driven proxy with no theta decay. Intra-trade option premiums rarely move > 15%, so no `tp_pct ≥ 0.15` ever triggers — every trade exits at REPLAY_SESSION_BOUNDARY/EOD instead. In LIVE, theta over 4-hour holding periods compounds with directional premium moves to make 20-50% intra-day premium drops common; the replay does not capture this.

**Decision:** No `tp_pct` config change. Defaults preserved. To actually tune PHX#185:
- (a) Backfill `indicator_bars` with real option-chain history (multi-day data engineering project), or
- (b) Wait until ~30 trading days of production data accumulate with the new TP1/give-back/decay code, then segment exit_attribution.jsonl by regime to drive recommendations from real fills.

Reproducer: `scripts/ops/run_replay_quiet.py --mode optimize --strategy ema20_strategy --underlying NIFTY --start-date 2026-02-23 --end-date 2026-04-30 --max-combos 600`. Analysis script: `scripts/ops/analyze_tp_discrimination.py`.

---

## 3. put_momentum_scalper

**Instruments:** NIFTY_IDX, BANKNIFTY_IDX | **Timeframe:** 5m + 15m trend | **Direction:** Long ATM PE
**Data:** 1,234 5m bars + 120 15m bars per underlying across 19 trading days

### Backtest Results Summary

#### NIFTY_IDX (limited signal universe — 2-3 trades in 19 days)
| Config | Trades | Wins | Win% | Total PnL% | PF |
|--------|--------|------|------|------------|------|
| **CURRENT** | 2 | 2 | 100% | +0.62% | inf |
| **RSI_20_42 (BEST)** | 3 | 3 | 100% | +0.89% | inf |
| AGGR | 3 | 2 | 66.7% | +0.82% | 3.62 |

#### BANKNIFTY_IDX (3 trades in 19 days)
| Config | Trades | Wins | Win% | Total PnL% | PF |
|--------|--------|------|------|------------|------|
| **CURRENT** | 3 | 3 | 100% | +0.63% | inf |
| **RSI_25_48 (BEST)** | 3 | 3 | 100% | +1.04% | inf |
| **SAFE** | 3 | 3 | 100% | +1.12% | inf |

### Optimized Parameters

```yaml
# put_momentum_scalper — strategy_env.yaml
- name: "put_momentum_scalper"
  enabled: false  # disabled in LIVE as of 2026-06-03 EMA20-only routing
  instruments:
    - label: "NIFTY_IDX"
      entry_start: "09:20"
      entry_end: "14:45"
      rsi_falling_bars_required: 1
      max_bars_in_trade: 14
    - label: "BANKNIFTY_IDX"
      entry_start: "09:20"
      entry_end: "14:45"
      rsi_falling_bars_required: 1
      max_bars_in_trade: 14
    - "FINNIFTY_IDX"
    - "SENSEX_IDX"
    - "MIDCPNIFTY_IDX"
  params:
    timeframe_seconds_5m: 300
    timeframe_seconds_15m: 900
    min_atr_ratio: 0.0008     # no change — ATR filter not the bottleneck
    trend_ema_tolerance_ratio: 0.0015
    rsi_min: 20               # CHANGED from 25 — widen floor to capture more oversold entries
    rsi_max: 45               # CHANGED from 40 — widen ceiling for moderate-weakness entries
    option_sl_pct: 0.25       # no change
    partial_tp_r: 1.0         # no change
    final_tp_r: 1.5           # no change
    rsi_falling_bars_required: 1  # CHANGED from 2 — single declining bar sufficient
    lookback_breakdown_bars: 8    # CHANGED from 10 — shorter lookback for more frequent signals
    volume_mult: 1.4          # no change
    max_bars_in_trade: 8      # CHANGED from 6 — allow more time for momentum to develop
    strike_mode: "ATM"
    lots_per_trade: 1
    morning_start: "09:20"
    morning_end: "11:00"
    afternoon_start: "13:30"
    afternoon_end: "14:45"
```

### Key Findings
- **Very few signals generated (2-3 per instrument in 19 days).** The 15m downtrend + RSI oversold + breakdown combination is extremely selective. All existing signals were profitable.
- **All exits are MAX_BARS timeout.** SL/TP never triggered — the put premium moves too slowly relative to the 25% SL threshold. Extend `max_bars_in_trade` from 6 to 8 to let profitable trades run longer.
- **RSI range [25-40] too narrow.** Widening to [20-45] captures 1 more profitable trade on NIFTY. On BANKNIFTY, [25-48] captures better-quality entries.
- **Lookback 10 bars may be too long.** Reducing to 8 increases breakdown frequency while maintaining quality.
- **Signal frequency is the primary issue.** With 2-3 trades per 19 days per instrument, statistical significance is low. Consider:
  - Relaxing the 15m trend filter (tolerance_ratio > 0.0015)
  - Running on more instruments (FINNIFTY, SENSEX, MIDCPNIFTY)
  - Accepting entries when RSI is merely declining (not yet in oversold zone)

### Environment Variables
```bash
# No specific env var overrides needed — configure in strategy_env.yaml
# Per-instrument overrides already supported via instruments list
```

---

## Cross-Strategy Observations

### Data Quality Notes
- **30s bars:** 13,798 bars with RSI, ATR, MACD, ADX, private EMA. Standard `ema_20` column is NULL (private EMA used instead).
- **5m bars:** 1,174 bars with all indicators populated including standard `ema_20`.
- **15m bars:** 456 bars (NIFTY/BANKNIFTY only — FINNIFTY/SENSEX/MIDCPNIFTY stopped after Feb 26).
- **`di_spread` column is NULL** in Postgres despite `plus_di` and `minus_di` being populated. Strategies compute spread inline.

### Historical Research Ranking (Not LIVE Enablement)
1. **exclusive_nifty_ce_buy** - PF=5.09 in the historical sweep, but disabled in LIVE.
2. **ema20_strategy NG_FUT** - PF=2.55 (EMA-8, strongest EMA20 instance).
3. **put_momentum_scalper** - PF=inf in the historical sweep, but too few trades for statistical validity and disabled in LIVE.
4. **ema20_strategy BANKNIFTY** - PF=1.08 (marginal edge).
5. **ema20_strategy NIFTY** - PF=0.96 (slightly negative, needs more data).

### Current LIVE Priority
1. **Maintain EMA20-only routing:** do not re-enable `exclusive_nifty_ce_buy`, `put_momentum_scalper`, or `nifty_weekly_credit_spreads` without fresh release evidence and operator approval.
2. **Apply with monitoring:** EMA20-only parameter changes such as NIFTY `min_atr` reduction remain candidates, but must be validated against current live data first.
3. **Investigate further:** EMA20 SL/TP mechanism. Historical replay rarely triggers premium-based exits; use real-fill or real-option data before changing exits.

### Caveats
- **19-day sample** is insufficient for robust parameter optimization. These findings should be validated over 60+ trading days.
- **Survivorship bias:** Only NIFTY/BANKNIFTY have full 19-day data. FINNIFTY/SENSEX/MIDCPNIFTY data ends Feb 26.
- **No slippage/commission modeled.** Real-world results will be ~0.5-1.5 pts worse per round trip on options.
- **Backtest uses underlying price movement** as a proxy for option premium change. Actual option P&L depends on moneyness, IV, theta, and greeks.
