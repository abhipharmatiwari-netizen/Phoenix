# Phoenix Strategies — Operator Reference

> **Status:** Authoritative end-to-end reference for every strategy registered in `app/strategies/registry.py`. Generated 2026-05-08.
> All defaults below come from `app/config/strategy_env.yaml`. Per-tenant overrides may be present in the `strategy_configs` Postgres table — when in doubt, check there first. Cited line numbers are accurate at the commit that introduced this doc; treat them as starting points if files have moved.

---

## Quick reference matrix

| # | Canonical ID | Display name | Production status | Underlyings (whitelist) | Direction | Timeframe | Source file |
|---|---|---|---|---|---|---|---|
| 1 | `ema20_strategy` | EMA 20 Strategy | **ACTIVE** | NIFTY, BANKNIFTY, NG_FUT | SELL CE | 5m | `app/strategies/ema20_strategy.py` |
| 2 | `exclusive_nifty_ce_buy` | Exclusive Nifty CE Buy | **ACTIVE** | NIFTY only | BUY CE | 30s | `app/strategies/exclusive_nifty_ce_buy.py` |
| 3 | `put_momentum_scalper` | Put Momentum Scalper | **ACTIVE** | NIFTY, BANKNIFTY | BUY PE | 5m + 15m | `app/strategies/put_momentum_scalper.py` |
| 4 | `nifty_weekly_credit_spreads` | Nifty Weekly Credit Spreads | **ACTIVE** (live since 2026-05-08) | NIFTY only | SELL spread / iron condor | 5m | `app/strategies/nifty_weekly_credit_spreads.py` |
| 5 | `put_buy` | Put Buy | _DISABLED_ | declared multi but not whitelisted | BUY PE | 5m + 15m | `app/strategies/put_buy_live.py` |
| 6 | `put_reversion_writer` | Put Reversion Writer | _DISABLED_ | not whitelisted | SELL PE | 5m + 15m | `app/strategies/put_reversion_writer.py` |
| 7 | `ce_orb` | CE ORB | _DISABLED / incomplete_ | not whitelisted | BUY CE | 1m | `app/strategies/ce_orb.py` |
| 8 | `ce_pdh_rb` | CE PDH RB | _DISABLED_ | declared but disabled | SELL CE | 1m | `app/strategies/ce_pdh_rb.py` |
| 9 | `option_strategy` | Option Strategy (template) | _DISABLED / framework_ | declared multi but disabled | configurable | configurable | `app/strategies/option_strategy.py` |
| 10 | `delta_strangle` | Delta Strangle | _DISABLED_ | NIFTY, BANKNIFTY (disabled) | SELL CE+PE | daily fixed | `app/strategies/delta_strangle.py` |

A strategy is "ACTIVE" when **all three** are true: (a) `strategies[].enabled: true` in `strategy_env.yaml`, (b) its name appears in `instruments.<UNDERLYING>.allowed_strategies` for at least one enabled underlying, (c) `strategy_class` is wired up in `multi_instrument_stream.py` (it is, for all 10).

---

## Glossary

| Term | Meaning |
|---|---|
| **R** | Risk unit — the rupee distance from entry to stop. `1R` exit = profit equal to risk; `2R` = double the risk. |
| **ATR** | Average True Range, typical volatility measure (typically 14-period). |
| **TP** | Take-profit target — the level at which the position is exited for profit. |
| **TP1** | Partial take-profit — book a fraction of the position at the first profit target; let the rest run. |
| **SL** | Stop-loss — exit level on the loss side. |
| **Trail SL** | Stop that ratchets in the favourable direction as price moves; never moves against the trade. |
| **Give-back** | If profit reaches a peak and retraces by a configured fraction, exit the position. Caps unrealised profit erosion. |
| **EOD** | End-of-day square-off — force-exit at a configured time. |
| **Soft SL** | Monitored on tick by the strategy; exit order is submitted via `place_order_via_bridge`. (No GTT placed at broker.) |
| **Regime** | Volatility/trend classification (CHOPPY, TRENDING, HIGH_VOL, NO_TRADE) — gates entries via `dynamic_policy`. |
| **ATM CE / ITM1 PE** | At-the-money call / one-strike-in-the-money put. |
| **OTM N steps** | N strikes out-of-the-money relative to spot. |

---

## Cross-cutting concerns (apply to every strategy)

### Exit retry & circuit breaker
Every active strategy wraps its exit submission with a retry circuit:
- `*_EXIT_RETRY_COOLDOWN_SECONDS` (default 5s) — gap between retry attempts after a transient failure.
- `*_EXIT_MAX_RETRIES` (default 12) — after N failures, the circuit opens.
- `*_EXIT_CIRCUIT_OPEN_SECONDS` (default 300s) — how long the circuit stays open before allowing retries again.
- `*_EXIT_CIRCUIT_ALERT_INTERVAL_SECONDS` (default 60s) — re-emit `[ALERT]` log lines while the circuit is open.

Variables exist with strategy-specific prefixes (`EMA20_`, `PUT_MOM_`, `PUT_BUY_`, `NIFTY_SPREAD_`).

### Position-ownership lock (structural exclusive exit lock — issue #200, shipped 2026-05-08)
Every strategy submits exits through `place_order_via_bridge → OrderRouter`. The router calls `PositionOwnershipStore.try_acquire`, which now treats **OWNED → RELEASING as an exclusive lock**: a second exit-side acquire on a contract whose record is already in RELEASING is refused with `PositionOwnershipDecision(allowed=False, reason="exit_already_in_flight")` until either (a) the broker confirms terminal fill (entry flattens, lock auto-releases) or (b) the watchdog window expires (env `POSITION_OWNERSHIP_EXIT_LOCK_MAX_SECONDS`, default 90s — logs `exit_lock_watchdog_expired` WARNING then permits the retry so ops surfaces the stuck lock instead of the position being permanently bricked). Protection is strategy-independent — `ema20_strategy`, `exclusive_nifty_ce_buy`, `put_momentum_scalper`, `nifty_weekly_credit_spreads`, and any future strategy benefit without per-strategy code. Watch: `exit_already_in_flight` INFO log line. See [issue #200](https://github.com/abhipharmatiwari-netizen/Phoenix/issues/200) and `tests/orders/test_position_ownership_exit_lock.py`.

### EMA20-specific guard (PR #201, shipped 2026-05-08)
`Ema20Strategy._pending_exit_by_label` blocks a second `_exit_position` call on the same `option_label` until the prior exit's fill is observed (or 60s watchdog elapses). Watch: `EMA20 exit suppressed (in-flight)` log line. See [`fix(ema20): per-label pending-exit guard prevents duplicate SL exits`](https://github.com/abhipharmatiwari-netizen/Phoenix/pull/201).

### Flip-the-trade fill protection (PR #199, shipped 2026-05-08)
Applies to **every** strategy. If a single broker fill exceeds the open quantity (carrying `net_qty` through zero), `OrderLifecycleService._apply_position_fill` routes the record to `RECONCILING` with state-reason `flip_fill_blocked:close_leg=N:open_leg=M`, instead of allowing `filled_qty_close > filled_qty_open`. Watch: `POSITION_FLIP_FILL_DETECTED` audit event. See [PR #199](https://github.com/abhipharmatiwari-netizen/Phoenix/pull/199).

### Dynamic policy (regime-based parameter overrides)
Several strategies (`ema20_strategy`, `exclusive_nifty_ce_buy`, `put_momentum_scalper`) load a regime-keyed policy block from `strategy_env.yaml` (`dynamic_policy.profiles.<REGIME>`) that can override `qty_mult`, `sl_pct`, `tp_pct`, `disable_entries`, etc. Regimes: `TRENDING`, `CHOPPY`, `HIGH_VOL`, `NO_TRADE`. Set `policy_id` in YAML; loaded from the `canonical_strategy_registry` table at runtime.

### Per-position trailing profit lock (account-level, deployed 2026-05-08)
Cuts across all strategies via `PositionTrailingLockEngine` (env: `POSITION_TRAILING_LOCK_ENABLED=true`). Independent of strategy-level trail logic; tracks each open position's peak unrealised P&L and exits when it falls below `peak × (1 − giveback_pct)` (default 10%) once peak ≥ ₹500. See [`feat(pnl): per-position trailing profit lock`](https://github.com/abhipharmatiwari-netizen/Phoenix/commit/e960ff4).

### Manual / external broker fills
Manual exits placed directly via the broker's UI bypass the strategies entirely. The `ExternalFillReconciler` (deployed 2026-05-08) detects them on every order-sync cycle and ingests them into `pnl_snapshots` under sentinel strategy `__external__`. See [`feat(orders): ingest external broker fills into pnl_snapshots`](https://github.com/abhipharmatiwari-netizen/Phoenix/commit/1b47813).

---

## 1. `ema20_strategy` — EMA 20 trend-fade short call

### Identity
- **Class:** `Ema20Strategy` in [app/strategies/ema20_strategy.py](../app/strategies/ema20_strategy.py)
- **Direction:** SELL ATM CE (short call on bearish trend confirmation).
- **Underlyings active:** NIFTY_IDX, BANKNIFTY_IDX, NG_FUT.

### Entry logic
A bar closes below `EMA(period)` with sufficient volatility and downward momentum:
1. **Trend filter:** `close < EMA(period)` on the 5-min timeframe.
2. **Volatility floor:** `ATR ≥ min_atr` (NIFTY 25 pts, BANKNIFTY 2 pts, NG 1.2 pts).
3. **RSI confirmation** (NIFTY only by default): RSI must be falling (`require_rsi_falling=true`).
4. **ADX filter** (off by default): if enabled, requires ADX ≥ `min_adx` and `+DI − -DI ≥ min_di_spread`.
5. **Regime gate:** `dynamic_policy` may set `disable_entries=true` for CHOPPY / NO_TRADE regimes.

**Time gates:** NIFTY 09:30–15:00 IST, NG 09:30–23:30 IST.
**Sizing:** `<UNDERLYING>_EMA20_LOTS` env var (default 1); regime-scaled via `qty_mult`.

### Stop-loss
- **Type:** Fixed % of entry price (soft SL, monitored on tick).
- **Default:** `sl_pct = 0.20` (20% of premium).

### Take-profit
- **Type:** Fixed % of entry price.
- **Default:** `tp_pct = 0.30`.
- **TP1 partial:** `tp1_pct` (target) + `tp1_qty_pct` (lots %) — disabled by default; enable per underlying.

### Trailing stop
- **Trigger:** Once profit ≥ `trail_trigger_pct` (default 10%).
- **Buffer:** `trail_buffer_pct` (default 5–8%, regime-dependent).
- **Mechanic:** Lock-only — the trail-SL never moves further from the underlying.
- **Give-back guard (PHX#183):** Once peak favourable ≥ `giveback_arm_pct`, exit if it retraces by `giveback_pct`.

### Other exits
- **EOD:** `square_off_time` (15:00 indices, 23:30 NG).
- **Signal flip:** `close > EMA(period)` invalidates the bear thesis → immediate exit.
- **Time-decay accelerator (PHX#184):** Within `decay_tighten_minutes_before_eod` of EOD, TP/trail tighten by `decay_tp_multiplier` / `decay_trail_buffer_multiplier`.

### Critical parameters
| Param | Default (NIFTY) | Where |
|---|---|---|
| `ema_period` | 20 | strategy_env.yaml NIFTY block |
| `min_atr` | 25.0 | NIFTY block |
| `sl_pct` | 0.20 | NIFTY block |
| `tp_pct` | 0.30 | NIFTY block |
| `trail_trigger_pct` | 0.10 | NIFTY block |
| `trail_buffer_pct` | 0.05 | NIFTY block |
| `policy_id` | `ema20_nifty_v1` | dynamic_policy section |
| `EMA20_PENDING_EXIT_MAX_SECONDS` | 60 | env (PR #201 watchdog) |

### Managed-position fields (`Ema20Position`)
`option_label, broker_symbol, exchange, symbol_token, qty, requested_lots, lot_size, broker_qty, entry_price, sl_price, tp_price, best_price, trail_active, entry_time, fill_price_confirmed, tp1_filled, original_qty, max_favorable_pct, giveback_armed, decay_window_applied`

### Known caveats
- **Authoritative-entry mode:** `ema20_is_authoritative=true` makes ema20 bypass selector regime gating — required to keep entries firing when the central regime selector says CHOPPY.
- **Per-label pending-exit guard active (PR #201).** Duplicate SL exits within 60s are silently suppressed.

### Entry decision tree (5-minute bar close)

The strategy evaluates 11 ordered gates per bar inside `on_bar` ([app/strategies/ema20_strategy.py:1086-1307](../app/strategies/ema20_strategy.py)). Any failed gate aborts that bar's entry attempt; passing all gates submits one short-CE order via `_short_call_once_per_bar` (line 1561-1743).

| # | Gate | Pass condition | Default / source | On fail |
|---|---|---|---|---|
| 1 | Dispatch | `label == underlying_label` AND `tf == signal_timeframe (300s)` | constructor | return |
| 2 | Square-off time | `now < square_off_time` | NIFTY 15:00 (yaml:282), NG 23:30 | force-exit-all + return |
| 3 | First-entry window | `now ≥ first_entry_time` | NIFTY 09:30 (yaml:281) | return |
| 4 | Regime entry-delay | `now ≥ first_entry_time + first_entry_delay_minutes` | dynamic 0–15 (yaml:343, 423) | return |
| 5 | EMA available | `ema_val ≠ None` AND `not isnan(ema_val)` | `ema_period=20` (yaml:272) | return (`missing_ema`) |
| 6 | Regime gate | `disable_entries == false` for current regime | dynamic policy | return (`policy_disable_entries`) |
| 7 | ATR floor | `ATR ≥ min_atr` | NIFTY 25.0, regime-overridable to 18.0 / 24.0 | return (`atr_too_low`) |
| 8 | RSI falling | `rsi_prev_prev > rsi_prev > rsi_current` | enforced when `require_rsi_falling=true` (yaml:275) | return (`rsi_not_falling`) |
| 9 | ADX/DI filter | `ADX ≥ min_adx` AND `(if require_bearish_di) -DI > +DI` AND `-DI − +DI ≥ min_di_spread` | only when `use_adx_filter=true` (yaml:276; on by default in NORMAL/CHOPPY/HIGH_VOL profiles) | return (`adx_filter_blocked`) |
| 10 | Trend confirmation | `close < ema_val` | tick close vs EMA20 | return (`close_not_below_ema`) |
| 11 | Submit | resolve qty (× `qty_mult`), build OrderRequest, route via `place_order_via_bridge`, freeze SL/TP/trail/decay/giveback params on the new `Ema20Position` | line 1561-1743 | — |

### Regime profiles (deep)

EMA20 reads its regime classification from `RegimeClassifier.update()` (line 1335-1399) using thresholds defined in `strategy_env.yaml`. The resulting regime overlays parameters via `DynamicPolicyEngine.apply()` (line 1382-1387).

**Authority — `ema20_is_authoritative: true` (yaml:821):** the central regime selector cannot block EMA20 entries — only EMA20's own per-regime `disable_entries` can. This was a fix from a March 2026 incident where the selector regime was blocking EMA20 even when its own NO_TRADE profile would have allowed reduced-size entries.

**NIFTY classifier thresholds (`ema20_nifty_v1`, yaml:293-304):**

| Threshold | Value | Used for |
|---|---|---|
| `adx_trend` | 24 | ADX line above which TRENDING is candidate |
| `di_spread_trend` | 4 | minimum +DI − -DI to confirm trend |
| `ema_slope_trend_min` | 0.0005 | EMA slope cutoff for trend |
| `atr_norm_high` | 1.45 | normalised ATR ⇒ HIGH_VOL |
| `atr_norm_secondary` | 1.2 | secondary high-vol breakpoint |
| `gap_ratio_spike` | 0.8 | gap-vs-ATR ratio for vol spikes |
| `atr_norm_low` | 0.7 | low-vol cutoff |
| `adx_normal_low / high` | 16 / 24 | ADX band defining NORMAL |
| `chop_adx_max` | 15 | ADX upper bound for CHOPPY |
| `chop_di_spread_max` | 2.5 | DI spread upper bound for CHOPPY |

**NIFTY per-regime overrides (yaml:305-356):**

| Param | TRENDING | NORMAL | CHOPPY | HIGH_VOL | NO_TRADE |
|---|---|---|---|---|---|
| `ema_period` | 20 | 20 | **30** | 20 | 20 |
| `use_adx_filter` | false | **true** | **true** | **true** | **true** |
| `min_adx` | 18 | 20 | **24** | **24** | 22 |
| `min_di_spread` | 0 | 0 | **8** | 0 | **5** |
| `require_rsi_falling` | true | true | true | true | true |
| `sl_pct` | 0.20 | 0.20 | 0.20 | 0.20 | 0.20 |
| `tp_pct` | 0.30 | 0.30 | 0.30 | 0.30 | 0.30 |
| `trail_trigger_pct` | **0.15** | 0.12 | **0.0** | **0.16** | 0.12 |
| `trail_buffer_pct` | 0.05 | 0.05 | **0.0** | **0.07** | 0.05 |
| `qty_mult` | 1.0 | 1.0 | **0.6** | **0.6** | **0.4** |
| `min_atr` | **24.0** | 21.0 | **18.0** | 24.0 | 24.0 |
| `first_entry_delay_minutes` | 0 | 0 | 0 | **10** | **15** |
| `disable_entries` | false | false | false | false | **false** (NIFTY) |

**Bold** = differs from NORMAL baseline.

**BANKNIFTY differences (`ema20_banknifty_v1`, yaml:375-435):**
- Stricter classifier: `adx_trend: 25, di_spread_trend: 5, atr_norm_high: 1.6, atr_norm_secondary: 1.3, gap_ratio_spike: 1.0, atr_norm_low: 0.8, adx_normal_low: 18, adx_normal_high: 25, chop_adx_max: 17, chop_di_spread_max: 3`.
- **`NO_TRADE.disable_entries: true`** (hard block) — unlike NIFTY which keeps `false` and just shrinks `qty_mult` to 0.4. BANKNIFTY refuses to fire in NO_TRADE; NIFTY can still attempt with reduced size.

### Tests
`tests/strategies/test_ema20_strategy.py`, `test_ema20_profit_booking.py`, `test_ema20_qty_resolution.py`, `test_ema20_pending_exit_guard.py`.

---

## 2. `exclusive_nifty_ce_buy` — Pullback long-call on NIFTY

### Identity
- **Class:** `ExclusiveNiftyCeBuyStrategy` in [app/strategies/exclusive_nifty_ce_buy.py](../app/strategies/exclusive_nifty_ce_buy.py)
- **Direction:** BUY ATM CE.
- **Underlyings active:** NIFTY only.

### Entry logic (30s timeframe)
Bullish pullback to EMA20 with momentum alignment:
1. **EMA proximity:** `|close − EMA20| ≤ ema_atr_buffer × ATR` (buffer 0.05 ATR).
2. **RSI:** `52 ≤ RSI ≤ 72`.
3. **MACD histogram:** `≥ macd_hist_min` (default 0.0 — i.e. positive or flat-rising).
4. **Optional ADX filter:** `ADX ≥ min_adx` (default 14, off unless enabled).
5. **Volatility-quantile gate:** Underlying ATR / close ≥ a configured quantile (default 0.45).

**Time gates:** session_start 10:15, last_entry 14:45, EOD 15:15.
**Sizing:** `lots_per_trade = 1`; regime-disabled in CHOPPY/NO_TRADE; HIGH_VOL `qty_mult = 0.6`.

### Stop-loss
- **Type:** ATR multiple below entry.
- **Default:** `sl_atr = 2.0` (HIGH_VOL profile widens to 2.5).

### Take-profit
- **Type:** ATR multiple above entry.
- **Default:** `tp_atr = 2.5` (HIGH_VOL narrows to 2.0).
- **Late session cap:** After 14:45, TP is capped at `late_tp_cap_atr = 2.6`.

### Trailing stop
- **Buffer:** `trail_cushion_atr = 0.16` ATR; late session uses `late_trail_cushion_atr = 0.08`.
- **Activation:** `trail_active_atr = 0.8`; late session `late_trail_active_atr = 0.6`.
- **Mechanic:** EMA-based with cushion; SL ratchets up only.

### Other exits
- **EOD:** 15:15.
- **EMA-failure exit:** If `ema_fail_count ≥ ema_fail_bars` (3) — i.e. 3 bars closing on the wrong side of EMA20.
- **Cooldown after failed entry:** `cooldown_bars = 2`.
- **Daily cap:** `max_trades_per_day = 1` per symbol.

### Critical parameters
| Param | Default | Notes |
|---|---|---|
| `timeframe_seconds` | 30 | 30-second bars |
| `sl_atr` / `tp_atr` | 2.0 / 2.5 | volatility-scaled |
| `rsi_min` / `rsi_max` | 52 / 72 | momentum window |
| `vol_quantile` | 0.45 | volatility regime gate |
| `ema_fail_bars` | 3 | invalidation count |

### Managed-position fields (`PositionState`)
`option_label, qty, entry_option_price, entry_underlying_price, entry_atr, sl_price, target_price, entry_time, broker_symbol, exchange, symbol_token`

### Known caveats
- One trade/day per symbol — does not re-enter after a TP, only a stop.
- Late-session TP cap mitigates expiry-day squeezes near 15:00.

### Entry decision tree (two-stage: bar + tick)

The strategy splits decision-making across two stages:
- **Stage 1 — bar-level signal** (30-second bars): evaluates all conditions and either sets `pending_entry_at` or returns.
- **Stage 2 — tick-level execution**: when a tick arrives at/after `pending_entry_at`, the position is opened on that tick's underlying price.

This separation lets entries fire on any tick within the next bar window, instead of waiting for the next bar close.

**Stage 1: bar gates** (`on_bar`, [line 1226-1381](../app/strategies/exclusive_nifty_ce_buy.py))

| # | Gate | Pass condition | Default / source | On fail |
|---|---|---|---|---|
| 1 | Dispatch | `label == underlying_label` AND `tf == 30` | yaml:592 | return |
| 2 | Daily reset & warm start | rotate session state at IST 00:00; load prior EMA / vol from PG / CSV | line 839-888 | continue |
| 3 | Regime — disable | `live_disable_entries == false` | dynamic policy | return (`policy_disable_entries`) |
| 4 | Daily trade cap | `trades_today < max_trades_per_day` | 1 (yaml:605) | return (`max_trades_reached`) |
| 5 | ATR floor | `ATR ≥ min_atr` | dynamic; default 0.0 | return (`min_atr_not_met`) |
| 6 | Buy signal | composite — see sub-table below | `_compute_buy_signal` line 1052-1196 | return (`no_buy_signal`) |
| 7 | Entry window | `session_start ≤ next_bar_start ≤ last_entry_time` AND `next_bar_start < squareoff_time` | 10:15 / 14:45 / 15:15 (yaml:611-613) | return (`outside_entry_window`) |
| 8 | Cooldown | `cooldown_bars == 0` | 2 (yaml:610) post-exit | return (`cooldown_active`) |
| 9 | Set pending entry | record `pending_entry_at = next_bar_start` | line 1370-1372 | — |

**Stage 1 gate 6 — composite buy signal (every sub-condition must hold):**

| Sub-gate | Condition | Default |
|---|---|---|
| vol_ok | `vol_20 ≥ vol_threshold` (rolling quantile) | quantile 0.45 (yaml:593) |
| trend_ok | `ema20 > ema50` (or `ema20 > ema50 > ema200` if `strict_trend`) | strict false (yaml:600) |
| rsi_rising | last 3 RSI strictly increasing | bar history |
| rsi_ok | `rsi_min < rsi < rsi_max` | 52–72 (yaml:595-596) |
| above_ema20 | `close > ema20 + ema_atr_buffer × atr` | buffer 0.05 (yaml:594) |
| macd_ok | `macd_cross_up AND hist ≥ macd_hist_min` OR `allow_near_macd AND macd_near_cross_up` | 0.0 / true (yaml:597-599) |
| mom_ok | `ret_1 > 0 AND ret_5 > 0` | log-returns |
| adx_ok | `adx ≥ min_adx` | 14.0 (yaml:601) |
| di_ok | `\|+DI − -DI\| ≥ min_di_spread` AND `+DI > -DI` | 0.0 (yaml:602) |

**Stage 2: tick execution** (`on_tick`, line 1201-1223)

| # | Gate | Pass condition | On fail |
|---|---|---|---|
| 10 | Pending entry trigger | `pending_entry_at ≠ None` AND `now ≥ pending_entry_at` | wait for next tick |
| 11 | Re-check entry window | `now` still inside session window | clear pending, return |
| 12 | Open position | select ATM CE → resolve qty (× `qty_mult`) → SL = `entry − sl_atr × atr` → TP = `entry + min(tp_atr, late_tp_cap_atr if after 14:45) × atr` → MARKET BUY via `place_order_via_bridge` | — |

### Regime profiles (deep)

Policy ID `exclusive_nifty_ce_v1` (yaml:624). Regime is classified by `AdaptivePolicyAdapter` ([line 172-207](../app/strategies/exclusive_nifty_ce_buy.py)) using thresholds at yaml:628-632 and applied at the start of every bar evaluation (line 1240-1249).

**Classifier thresholds (yaml:628-632):**

| Threshold | Value | Used for |
|---|---|---|
| `adx_trend` | 25 | ADX cutoff for TRENDING |
| `di_spread_trend` | 6 | DI confirmation for trend |
| `atr_norm_high` | 1.5 | normalised ATR ⇒ HIGH_VOL |
| `chop_adx_max` | 20 | upper-bound ADX for CHOPPY |

**Per-regime overrides (yaml:633-659):**

| Param | TRENDING | NORMAL | CHOPPY | HIGH_VOL | NO_TRADE |
|---|---|---|---|---|---|
| `disable_entries` | false | false | **true** | false | **true** |
| `min_adx` | **12.0** | 14.0 | — | **20.0** | — |
| `min_di_spread` | 0.0 | 0.0 | — | 0.0 | — |
| `sl_atr` | 2.0 | 2.0 | — | **2.5** | — |
| `tp_atr` | 2.5 | 2.5 | — | **2.0** | — |
| `qty_mult` | 1.0 | 1.0 | — | **0.6** | — |
| `rsi_min` | **50.0** | 52.0 | — | **55.0** | — |
| `rsi_max` | 72.0 | 72.0 | — | **68.0** | — |

**Bold** = differs from NORMAL. "—" = not overridden (regime is hard-blocked, so other params are irrelevant).

**Authority:** Uses its own dynamic policy. CHOPPY and NO_TRADE both set `disable_entries: true` — hard block at gate 3 above (no entry attempt at all). HIGH_VOL widens SL, narrows TP, halves position size, and tightens RSI band — entries only on stronger momentum during volatile sessions.

### Tests
`tests/strategies/test_exclusive_nifty_ce_buy.py`.

---

## 3. `put_momentum_scalper` — 5m breakdown long-PE under 15m downtrend

### Identity
- **Class:** `PutMomentumScalperStrategy` in [app/strategies/put_momentum_scalper.py](../app/strategies/put_momentum_scalper.py)
- **Direction:** BUY ATM/ITM1 PE.
- **Underlyings active:** NIFTY_IDX, BANKNIFTY_IDX (declared also for FINNIFTY/SENSEX/MIDCPNIFTY but those instruments are disabled).

### Entry logic (5m + 15m)
1. **15m bear filter:** `close < EMA20 × (1 + trend_ema_tolerance_ratio)`.
2. **5m EMA stack:** `close < EMA20` AND `close < EMA50`.
3. **5m RSI:** in `[rsi_min, rsi_max]` (default 20–45) AND falling for N bars.
4. **5m MACD:** `MACD < signal` AND `hist < 0` AND fresh negative crossover.
5. **5m ATR ratio:** `ATR / close ≥ min_atr_ratio` (default 0.0008).
6. **5m breakdown bar:** lowest low in `lookback_breakdown_bars` (8) AND lower-wick ratio ≤ 0.30.

**Time gates:** Per-instrument entry windows in `strategies[].instruments.<UNDERLYING>` (e.g. NIFTY 09:20–14:45).
**Sizing:** `lots_per_trade = 1`; regime-scaled.

### Stop-loss
- **Type:** Fixed % below entry premium.
- **Default:** `option_sl_pct = 0.25` (25%).

### Take-profit
- **Two-stage R-multiple:**
  - `partial_tp_r = 1.0` — closes at 1R profit.
  - `final_tp_r = 1.5` — closes at 1.5R.
- **Note:** as of this writing both are full-position exits; partial booking infrastructure is in place but not wired to split lots.

### Trailing stop
- **None implemented.** SL & TP only. Position is invalidated by structural events (see below).

### Other exits
- **EOD:** 15:20.
- **Trend invalidation:** `close > breakdown_high` (the swing high of the breakdown bar) → exit.
- **EMA-recovery invalidation:** `close > EMA20` on 5m → exit.
- **Time stop:** `max_bars_in_trade = 8` bars (40 min on 5m).

### Critical parameters
| Param | Default | Notes |
|---|---|---|
| `timeframe_seconds_5m` / `_15m` | 300 / 900 | dual-tf logic |
| `option_sl_pct` | 0.25 | premium SL |
| `partial_tp_r` / `final_tp_r` | 1.0 / 1.5 | R multiples |
| `min_atr_ratio` | 0.0008 | volatility floor |
| `lookback_breakdown_bars` | 8 | swing-low window |
| `max_bars_in_trade` | 8 | time stop |
| `policy_id` | `put_momentum_nifty_banknifty_v1` | regime block |

### Managed-position fields (`OptionPosition`)
`label, qty, entry_price, stop_price, partial_tp, final_tp, entry_time, entry_bar_index, breakdown_high, broker_symbol, exchange, symbol_token`

### Known caveats
- **No trailing logic** — relies on hard TP/SL/structural invalidation. Worth considering a trailing layer if win-rate is high but average win is small.
- **Volume filter not wired:** `volume_mult` parameter exists but is currently ignored.
- **VWAP is close-only** (lightweight proxy — no per-tick volume integration).

### Entry decision tree (15m → 5m two-timeframe)

The strategy uses 15-minute bars for trend confirmation and 5-minute bars for entry timing. Both timeframes are subscribed; each has its own bar handler.

**15m handler** (`_handle_15m_bar`, [line 914-960](../app/strategies/put_momentum_scalper.py)) — sets one boolean read by the 5m handler.

| # | Step | Pass condition | Default | Effect |
|---|---|---|---|---|
| 1 | EMA20 available | `ema_20 ≠ None` | indicator | else `state.trend_15m_down = false`, return |
| 2 | Compute trend | `state.trend_15m_down = (close ≤ ema20 × (1 + trend_ema_tolerance_ratio))` | tolerance 0.0015 (yaml:680) | stored on `InstrumentState` for the 5m gate to read |

**5m handler** (`_handle_5m_bar`, line 963-1215) — 14 ordered gates plus order submit:

| # | Gate | Pass condition | Default / source | On fail |
|---|---|---|---|---|
| 1 | Position-already-open | `state.position is None` | per-instrument | manage exits + return |
| 2 | Regime — disable | `live_disable_entries == false` | dynamic policy | return (`policy_disable_entries`) |
| 3 | Entry window | `now ∈ [morning_start, morning_end] ∪ [afternoon_start, afternoon_end]` | NIFTY 09:20–11:00, 13:30–14:45 (yaml:692-695) | return (`outside_entry_window`) |
| 4 | 15m downtrend | `state.trend_15m_down == true` | from 15m handler | return (`trend_15m_not_down`) |
| 5 | EMA20 / EMA50 available | both not None | indicators | return (`missing_indicator`) |
| 6 | Below both EMAs | `close < ema20 AND close < ema50` | 5m close | return (`close_not_below_ema20_ema50`) |
| 7 | VWAP filter | `close < vwap` (rolling-mean proxy) | line 999-1002 | return (`close_not_below_vwap`) |
| 8 | RSI in range | `rsi_min ≤ rsi ≤ rsi_max` | 20–45 (yaml:681-682) | return (`rsi_out_of_range`) |
| 9 | RSI falling | last `rsi_falling_bars_required` bars strictly decreasing | 1 (yaml:686, default 2) | return (`rsi_not_falling`) |
| 10 | MACD available | `macd, macd_signal, macd_hist` all not None | indicators | return (`missing_indicator`) |
| 11 | MACD bearish | `macd < macd_signal AND macd_hist < 0` | indicators | return (`macd_not_bearish`) |
| 12 | Fresh negative cross | prev-bar `hist ≥ 0` AND current-bar `hist < 0` | line 516-518 | return (`macd_no_fresh_negative_cross`) |
| 13 | ATR ratio floor | `atr / close ≥ min_atr_ratio` | 0.0008 (yaml:679) | return (`atr_below_threshold`) |
| 14 | Breakdown bar | lowest close in last `lookback_breakdown_bars` AND lower-wick / range ≤ 0.30 | 8 bars (yaml:687) | return (`breakdown_not_confirmed`) |
| 15 | Submit | select ATM PE → resolve qty → SL/TP from `option_sl_pct`, `partial_tp_r`, `final_tp_r` (yaml:683-685) → MARKET BUY via `place_order_via_bridge` | line 1236-1355 | — |

### Regime profiles (deep)

Policy ID `put_momentum_nifty_banknifty_v1` (yaml:698). The strategy DOES classify regime (via `_adaptive_policy.refresh`, line 893-897) for observability and logging, but **`profiles: {}`** in YAML at line 707 — meaning **no regime-specific parameter overrides** are applied. All entry parameters are static.

**Classifier thresholds (yaml:702-706, used for logging / selector routing only):**

| Threshold | Value |
|---|---|
| `adx_trend` | 22 |
| `di_spread_trend` | 8 |
| `atr_norm_high` | 1.5 |
| `chop_adx_max` | 18 |

**Routing-level regime gating (central selector, yaml:827-833):** the selector dispatches put_momentum_scalper bars to this strategy ONLY when regime ∈ `{TRENDING, HIGH_VOL}` for both NIFTY and BANKNIFTY. In CHOPPY / NO_TRADE the selector silently routes elsewhere — the strategy's `on_bar` is never called. So the regime gate, although not implemented inside the strategy, is enforced one layer above it.

**Entry-freeze keys** (captured at entry time, immune to mid-trade regime drift, line 163-168):
`min_atr_ratio, rsi_min, rsi_max, lookback_breakdown_bars` — once an entry fires, the regime that admitted it is locked for the trade's lifetime. Subsequent regime drift cannot re-tighten or re-loosen these gates mid-position.

**Why no per-regime overrides?** Per-regime parameter tuning was deferred. The current design relies entirely on the selector's routing decision (which regimes admit the strategy at all) plus static parameters. Adding regime profiles is on the backlog as a future enhancement.

### Tests
`tests/strategies/test_put_momentum_scalper.py`.

---

## 4. `nifty_weekly_credit_spreads` — regime-classified weekly credit spreads

### Identity
- **Class:** `NiftyWeeklyCreditSpreadStrategy` in [app/strategies/nifty_weekly_credit_spreads.py](../app/strategies/nifty_weekly_credit_spreads.py)
- **Direction:** SELL credit spread (PUT_SPREAD / CALL_SPREAD / IRON_CONDOR), each with a long hedge leg.
- **Underlyings active:** NIFTY only. **Live since 2026-05-08** (was structurally idle before — see commit `a1b55de`).

### Entry logic (5m)
Three regime branches, all gated on a sane implied-move check:
- **Implied-move sanity:** ATM straddle `atm_ce + atm_pe` as a fraction of spot must be in `[0.014, 0.030]`. Outside this band → no trade.

| Regime | 5m close vs EMA20 | RSI window | MACD hist | Spread fired |
|---|---|---|---|---|
| Bullish | above EMA20 | `[55, 70]` | `> 0` | `PUT_SPREAD` (short OTM PE, long deeper OTM PE) |
| Bearish | below EMA20 | `[30, 45]` | `< 0` | `CALL_SPREAD` (short OTM CE, long deeper OTM CE) |
| Sideways | — | `[45, 55]` | `\|hist\| ≤ flat_factor × close` AND flat range ≤ `flat_range_max_pct` | `IRON_CONDOR` (both verticals) |

**Time gates:** entry_start 10:10, entry_end 13:30 IST.
**Expiry:** `min_days_to_expiry = 1`, `max_days_to_expiry = 5`.
**Spread geometry:** short leg `0.8%` OTM, width `1.5%` (verticals); condor short legs `1.2%` OTM.
**Credit floors:** vertical credit ≥ 35% of width; condor total credit ≥ 40% of width sum.

### Stop-loss
- **Type:** Multiple of entry credit.
- **Default:** `stop_loss_mult_credit = 1.8` — exit when mark-to-market loss = 1.8 × entry credit.
- **Soft** — monitored on every mark refresh.

### Take-profit
- **Type:** Fraction of entry credit.
- **Default:** `take_profit_pct_of_credit = 0.60` — exit when 60% of credit is captured.

### Trailing stop
- **None.** Credit spreads have a defined max loss; trailing is unnecessary.

### Other exits
- **Expiry-day flatten:** `force_flatten_on_expiry = true` at `exit_time_eod = 15:15`.
- **Per-leg retry circuit:** legs unwind independently; `short_exit_done` and `long_exit_done` track each leg.

### Critical parameters
| Param | Default |
|---|---|
| `entry_start_time` / `entry_end_time` | 10:10 / 13:30 |
| `min_days_to_expiry` / `max_days_to_expiry` | 1 / 5 |
| `atm_straddle_min_pct` / `_max_pct` | 0.014 / 0.030 |
| `short_otm_pct` / `condor_short_otm_pct` | 0.008 / 0.012 |
| `spread_width_pct` | 0.015 |
| `min_credit_pct_of_width` | 0.35 |
| `take_profit_pct_of_credit` | 0.60 |
| `stop_loss_mult_credit` | 1.8 |
| `max_open_spreads` | 2 |
| `risk_per_trade_pct` / `max_total_risk_pct` | 1% / 5% |
| `account_equity` | 20000 |

### State (`SpreadLeg`, `OpenSpread`)
- **Leg:** `label, side, strike, qty, entry_price, broker_symbol, exchange, symbol_token`.
- **Spread:** `spread_id, spread_type, short_leg, long_leg, entry_credit, width_pts, max_loss_rupees, entry_time, expiry, extra_legs, short_exit_done, long_exit_done`.

### Known caveats
- **Strategy was structurally idle until 2026-05-08** — `enabled: false` flag in YAML and absence from `NIFTY_IDX.allowed_strategies` meant the stream worker never instantiated it. Both fixed in commit `a1b55de`.
- **Account equity is hard-coded in YAML** (`account_equity: 20000`) — risk sizing depends on this. Update when capital changes.
- **Live but signal-rare:** strict regime + ATM-straddle band means most days produce zero trades.

### Tests
`tests/strategies/test_nifty_weekly_credit_spreads.py`.

---

## 5. `put_buy` — Pullback-reversion long PE (DISABLED)

### Identity
- **Class:** `PutBuyLiveStrategy` in [app/strategies/put_buy_live.py](../app/strategies/put_buy_live.py)
- **Direction:** BUY ATM/ITM1 PE.
- **Production status:** _Disabled_ (`enabled: false` in YAML and not in any `allowed_strategies`).

### Entry logic (5m + 15m)
1. **15m bear filter:** `close < EMA50` AND `RSI < 50`.
2. **5m pullback:** within `pullback_band × ATR` of EMA21.
3. **5m stall:** ≥ `stall_candles` (3) bars closing at-or-below EMA21.
4. **5m trigger:** `close < EMA9` AND MACD-hist `< 0` AND falling AND `RSI < 40`.
5. **Delayed-entry buffer:** `entry_delay_bars = 1` after trigger.

### Stop-loss
- **Type:** Volatility-based (max of pullback-high, entry + `atr_mult × ATR`).

### Take-profit
- **Two-stage R:** `tp1_R ≈ 1.0` (partial 50%), `tp2_R ≈ 2.0` (full).

### Trailing stop
- **Active after TP1.** References EMA9 and previous high; SL tightens to entry on TP1 fill.

### Other exits
- **EOD:** 15:30.
- **Daily loss limit, max consecutive losses, max trades per day** — all configurable circuit breakers.

### Known caveats
- **Disabled in production.** Re-enabling requires verifying the volume-filter stub (currently warns but does nothing) and re-validating R-multiples.
- Daily-state reset already implemented (`trades_taken_today`, `realized_R_today`, `consecutive_losses`).

### Tests
`tests/strategies/test_put_buy_live.py`, `test_put_buy_strategy_rules.py`.

---

## 6. `put_reversion_writer` — Failed-breakdown short PE (DISABLED)

### Identity
- **Class:** `PutReversionWriterStrategy` in [app/strategies/put_reversion_writer.py](../app/strategies/put_reversion_writer.py)
- **Direction:** SELL OTM-1 PE.
- **Production status:** _Disabled_.

### Entry logic
1. Detect support: lowest low in `support_lookback_bars = 20`.
2. Breakdown: close < `support − breakdown_buffer_pct (0.15%)`.
3. RSI rises above `rsi_recovery_level = 45` after the breakdown low → "failed breakdown".
4. RSI in `[rsi_recovery_level, +range]` confirms recovery — short PE 1 step OTM.

**Time gates:** morning 10:00–11:00, afternoon 13:30–14:15.

### Stop-loss / Take-profit
- **SL:** 35% of premium.
- **TP:** 40% theta-decay (close PE for 60% remaining premium).

### Other exits
- **Time stop:** `max_bars_in_trade = 10`.
- **Invalidation:** new low below breakdown_low − `invalidation_buffer_pct = 0.20%`.

### Known caveats
- **Disabled.** Re-enabling requires param re-tuning for current vol regime and re-testing the support-detection logic.

### Tests
`tests/strategies/test_put_reversion_writer.py`.

---

## 7. `ce_orb` — Opening Range Breakout long CE (DISABLED / incomplete)

### Identity
- **Class:** `CeOrbStrategy` in [app/strategies/ce_orb.py](../app/strategies/ce_orb.py)
- **Direction:** BUY ATM CE.
- **Production status:** _Disabled and incomplete._ Not in any `strategies[]` block — no defaults configured.

### Entry logic (sketch)
1. Identify ORB (opening range) high/low in the IB window.
2. Trend on: cross above ORB high AND above VWAP.
3. Pullback retest of VWAP from above.
4. Continuation entry above ORB high.

### State (`SymbolState`)
`orb_high, orb_low, orb_done, trend_on, pullback_seen, breakout_price, vwap_sum_pv/sum_v, last_vwap, trades_today, position, last_reset_date`.

### Known caveats
- **Implementation incomplete.** Defaults missing from YAML; `instruments.allowed_strategies` does not include it.
- Tests exist (`test_ce_orb.py`, `test_ce_orb_hub_tokens.py`) but the strategy is not wired into LIVE.

---

## 8. `ce_pdh_rb` — Previous-Day-High range breakdown short CE (DISABLED)

### Identity
- **Class:** `CePdhRangeBreakoutStrategy` in [app/strategies/ce_pdh_rb.py](../app/strategies/ce_pdh_rb.py)
- **Direction:** SELL ATM CE.
- **Production status:** _Disabled._

### Entry logic (1m bars)
1. **PDH:** previous day's high.
2. **IB window:** ends 10:15. Compute IB high/low.
3. **Preconditions:** `gap ≤ max_gap_abs_pct (0.3%)`, `IB_range ≤ max_ib_range_pct (0.5%)`, breakout body ≥ `min_breakout_body_pct (0.3%)`.
4. **Entry:** `close > PDH − pre_breakout_band_pct (0.4%)` after 10:15 → short CE.

**Time gates:** first_entry 10:15, last_entry 14:00, EOD 15:20.

### Stop-loss / Take-profit
- **SL:** 25%, **TP:** 40% (premium-based).

### Other exits
- **Daily cap:** `max_trades_per_day_per_symbol = 2`.

### Known caveats
- **Disabled.** Requires verifying PDH availability in `instrument_meta` (or external feed) and re-validating preconditions on current data.

### Tests
`tests/strategies/test_ce_pdh_rb.py`.

---

## 9. `option_strategy` — Generic spread-template framework (DISABLED)

### Identity
- **Class:** `OptionStrategy` in [app/strategies/option_strategy.py](../app/strategies/option_strategy.py)
- **Direction:** Configurable per leg.
- **Production status:** _Disabled framework_, not active — provides spread-template machinery for other strategies to compose if needed.

### Entry logic
Regime-classified spread-template selection: ATR/close + RSI + MACD → choose template (SHORT_CE, SHORT_PE, SHORT_STRADDLE, SHORT_STRANGLE, vertical credit spreads). Per-template config drives strikes, sides, and qty multipliers.

### Stop-loss / Take-profit / Trail
Per-leg (`sl_pct`, `tp_pct`, `trail_buffer_pct`) — all soft, monitored per leg.

### State
- **Leg:** `OpenLegPosition` (`label, role, side, qty, entry_price, sl_price, tp_price, trail_buffer_pct, best_price, template_name, underlying, broker_symbol, exchange, symbol_token`).
- **Templates:** `SpreadTemplate`, `LegTemplate`.

### Known caveats
- **High configuration complexity** — requires careful per-underlying & per-regime tuning before any LIVE deployment.

### Tests
`tests/strategies/test_option_strategy.py`.

---

## 10. `delta_strangle` — Daily fixed-time delta-neutral strangle (DISABLED)

### Identity
- **Class:** `DeltaStrangleStrategy` in [app/strategies/delta_strangle.py](../app/strategies/delta_strangle.py)
- **Direction:** SELL CE + SELL PE (short strangle).
- **Production status:** _Disabled._

### Entry logic
- One trade per day inside `entry_start_ist` (09:20) – `entry_end_ist` (15:00).
- Pick CE and PE strikes near target delta (default 0.20 each).
- Short both legs.
- **No indicators** — pure delta-targeting.

### Stop-loss / Take-profit
- **SL:** 30% per leg, **TP:** 30% per leg.

### State (`OpenLeg`)
`label, side, qty_in_lots, qty, entry_price, sl_price, tp_price, entry_time, role, broker_symbol, exchange, symbol_token`.

### Known caveats
- **Disabled.** Requires a working delta lookup (broker option-chain data with greeks, or a model-derived delta).
- No regime/trend filter — naked premium-collection strategy; profitability depends entirely on realised vs implied vol.

### Tests
`tests/strategies/test_delta_strangle.py`.

---

## File map (where the work happens)

| Concern | File |
|---|---|
| Strategy registry (canonical names + class refs) | `app/strategies/registry.py` |
| Per-strategy parameter defaults | `app/config/strategy_env.yaml` |
| Per-tenant parameter overrides | Postgres `strategy_configs` table |
| Strategy instantiation in stream worker | `app/runners/multi_instrument_stream.py` (around line 1783, 2596+) |
| Order submission (used by every strategy) | `app/orders/strategy_bridge.py` → `app/orders/router.py` |
| Position ownership lock (one per contract) | `app/orders/position_ownership.py` |
| Order lifecycle state machine | `app/orders/order_lifecycle.py` |
| Position state machine (FLAT/OPEN/RECONCILING/...) | `app/core/position_state.py` |
| Per-position trailing profit lock (cross-strategy) | `app/pnl/position_trailing_lock.py` |
| External-fill reconciler (manual broker exits) | `app/orders/external_fill_reconciler.py` |
| Trade-decision lineage table | Postgres `trade_decision_lineage` |
| Tests | `tests/strategies/*.py` |

## Open follow-ups

- **Strategy reactivation backlog** — `ce_orb`, `ce_pdh_rb`, `put_buy`, `put_reversion_writer`, `delta_strangle` all need re-validation on current market data + parameter retuning before re-enable.

## Resolved (recent)

- **Issue [#200](https://github.com/abhipharmatiwari-netizen/Phoenix/issues/200)** — `PositionOwnershipStore.try_acquire` now enforces `OWNED → RELEASING` as an exclusive lock with watchdog (default 90s). Closed 2026-05-08.
- **Issue [#197](https://github.com/abhipharmatiwari-netizen/Phoenix/issues/197)** — Flip-the-trade fills routed to `RECONCILING` with structured `flip_fill_blocked` reason (PR #199). Closed 2026-05-08.
- **Issue [#198](https://github.com/abhipharmatiwari-netizen/Phoenix/issues/198)** — `POST /admin/state/clear-position-record` endpoint shipped (PR #199). Closed 2026-05-08.
