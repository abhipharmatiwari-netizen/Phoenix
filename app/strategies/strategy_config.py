from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StrategyConfig:
    # ---- Capital + risk ----
    account_equity: float = 100000.0
    risk_per_trade_pct: float = 0.01
    max_trades_per_day: int = 2
    daily_loss_limit_R: float = 2.0
    max_consecutive_losses: int = 2

    # ---- Session filters (Asia/Kolkata) ----
    tz: str = "Asia/Kolkata"
    session_start: str = "09:15"
    session_end: str = "15:30"
    no_trade_until: str = "09:30"
    last_entry_time: str = "14:45"
    eod_exit_time: str = "15:20"

    # ---- 15m indicators ----
    ema_15m_period: int = 50
    rsi_15m_period: int = 14
    atr_15m_period: int = 14
    use_atr_filter: bool = False
    atr_min_percentile: float = 20.0
    atr_percentile_lookback: int = 200

    # ---- 5m indicators ----
    ema_5m_fast: int = 9
    ema_5m_slow: int = 21
    rsi_5m_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_5m_period: int = 14
    use_volume_filter: bool = False
    vol_sma_period: int = 20

    # ---- Entry setup ----
    pullback_band: float = 0.25
    stall_candles: int = 1
    rsi_entry: float = 45.0
    strict_rsi_cross: bool = True

    # ---- Stop/target ----
    atr_mult: float = 0.6
    tp1_R: float = 0.8
    tp2_R: float = 1.3
    partial_take_pct: float = 0.6

    # ---- Options mapping (optional) ----
    strike_step: int = 50
    option_moneyness: str = "ATM"  # "ATM" or "ITM1"
    expiry_mode: str = "NEAREST"  # "NEAREST", "0DTE", "1DTE"
    lot_size: int = 50
    max_spread_pct: float = 0.01
    min_option_volume: int = 0
    min_open_interest: int = 0
    option_entry_price_field: str = "open"
    option_exit_price_field: str = "close"
    option_time_tolerance_seconds: int = 180

    # ---- Misc ----
    entry_delay_bars: int = 1
