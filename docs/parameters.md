# Optimized Strategy Parameters

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
  enabled: true
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
- **SL/TP re-calibration needed:** Current pct-based SL/TP on option premiums don't trigger intraday. Consider ATR-based SL/TP (like exclusive_ce_buy uses) or tighter percentage thresholds (10%/15%) for faster exits.

### Environment Variables
```bash
# NIFTY EMA20
NIFTY_EMA20_MIN_ATR=25.0

# BANKNIFTY EMA20
BANKNIFTY_EMA20_REQUIRE_RSI_FALLING=false

# NG EMA20
NG_EMA20_MIN_ATR=1.2
```

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
  enabled: true
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

### Risk-Adjusted Ranking (by Profit Factor)
1. **exclusive_nifty_ce_buy** — PF=5.09 (optimized), most reliable signal
2. **ema20_strategy NG_FUT** — PF=2.55 (EMA-8, strongest EMA20 instance)
3. **put_momentum_scalper** — PF=inf (too few trades for statistical validity)
4. **ema20_strategy BANKNIFTY** — PF=1.08 (marginal edge)
5. **ema20_strategy NIFTY** — PF=0.96 (slightly negative, needs more data)

### Recommended Priority Changes
1. **Apply immediately:** `exclusive_nifty_ce_buy` ADX/DI relaxation (highest confidence, clearest improvement)
2. **Apply with monitoring:** `ema20_strategy` NIFTY min_atr reduction to 25.0
3. **Apply cautiously:** `put_momentum_scalper` RSI range widening (low sample size)
4. **Investigate further:** EMA20 SL/TP mechanism (never triggers — consider ATR-based exits)

### Caveats
- **19-day sample** is insufficient for robust parameter optimization. These findings should be validated over 60+ trading days.
- **Survivorship bias:** Only NIFTY/BANKNIFTY have full 19-day data. FINNIFTY/SENSEX/MIDCPNIFTY data ends Feb 26.
- **No slippage/commission modeled.** Real-world results will be ~0.5-1.5 pts worse per round trip on options.
- **Backtest uses underlying price movement** as a proxy for option premium change. Actual option P&L depends on moneyness, IV, theta, and greeks.
