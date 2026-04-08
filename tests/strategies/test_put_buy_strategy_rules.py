import pandas as pd

from app.strategies.strategy_config import StrategyConfig
from app.strategies.strategy_put_buy import PutBuyStrategy


def _ts(date_str: str, time_str: str) -> pd.Timestamp:
    return pd.Timestamp(f"{date_str} {time_str}", tz="Asia/Kolkata")


def _row(
    ts: pd.Timestamp,
    open_px: float,
    high_px: float,
    low_px: float,
    close_px: float,
    ema9: float,
    ema21: float,
    rsi14: float,
    macd_hist: float,
    atr14: float,
    volume: float = 100.0,
    vol_sma: float = 100.0,
):
    return {
        "timestamp": ts,
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "close": close_px,
        "volume": volume,
        "ema9": ema9,
        "ema21": ema21,
        "rsi14": rsi14,
        "macd_hist": macd_hist,
        "atr14": atr14,
        "vol_sma": vol_sma,
    }


def _bear_15m_df(date_str: str, bearish: bool) -> pd.DataFrame:
    if bearish:
        close, ema50, rsi14 = 90.0, 100.0, 40.0
    else:
        close, ema50, rsi14 = 110.0, 100.0, 55.0
    return pd.DataFrame(
        [
            {
                "timestamp": _ts(date_str, "09:30"),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "ema50": ema50,
                "rsi14": rsi14,
                "atr14": 5.0,
                "atr_threshold": 1.0,
            }
        ]
    )


def test_bear_filter_blocks_trade():
    config = StrategyConfig()
    strategy = PutBuyStrategy(config)
    date_str = "2025-01-02"
    df_5m = pd.DataFrame(
        [
            _row(_ts(date_str, "09:35"), 100, 101, 99, 100, 101, 100, 46, -0.05, 4),
            _row(_ts(date_str, "09:40"), 100, 102, 99, 100, 101, 100, 44, -0.10, 4),
            _row(_ts(date_str, "09:45"), 100, 101, 99, 100, 101, 100, 43, -0.08, 4),
            _row(_ts(date_str, "09:50"), 98, 101, 96, 97, 100, 99, 42, -0.12, 4),
        ]
    )
    df_15m = _bear_15m_df(date_str, bearish=False)
    trades_df, _ = strategy.run_backtest(df_5m, df_15m)
    assert trades_df.empty


def test_entry_trigger_creates_trade():
    config = StrategyConfig()
    strategy = PutBuyStrategy(config)
    date_str = "2025-01-02"
    df_5m = pd.DataFrame(
        [
            _row(_ts(date_str, "09:35"), 100, 101, 99, 100, 101, 100, 46, -0.05, 4),
            _row(_ts(date_str, "09:40"), 100, 102, 99, 100, 101, 100, 44, -0.10, 4),
            _row(_ts(date_str, "09:45"), 100, 101, 99, 100, 101, 100, 43, -0.08, 4),
            _row(_ts(date_str, "09:50"), 98, 101, 96, 97, 100, 99, 42, -0.12, 4),
        ]
    )
    df_15m = _bear_15m_df(date_str, bearish=True)
    trades_df, _ = strategy.run_backtest(df_5m, df_15m)
    assert len(trades_df) == 1
    assert trades_df.iloc[0]["entry_time"] == _ts(date_str, "09:45")


def test_stop_calc_structure_vs_atr():
    strategy = PutBuyStrategy()
    stop_1 = strategy._calculate_stop_price(100, 105, 2)
    assert stop_1 == 105
    stop_2 = strategy._calculate_stop_price(100, 101, 5)
    assert stop_2 == 103


def test_tp1_moves_sl_to_be():
    strategy = PutBuyStrategy()
    date_str = "2025-01-03"
    df_5m = pd.DataFrame(
        [
            _row(_ts(date_str, "09:35"), 100, 101, 99, 100, 101, 100, 46, -0.05, 5),
            _row(_ts(date_str, "09:40"), 100, 104, 99, 100, 101, 100, 44, -0.10, 5),
            _row(_ts(date_str, "09:45"), 100, 101, 96.5, 99, 100, 99, 43, -0.08, 5),
            _row(_ts(date_str, "09:50"), 98, 101, 94, 95, 99, 98, 42, -0.12, 5),
        ]
    )
    df_15m = _bear_15m_df(date_str, bearish=True)
    trades_df, _ = strategy.run_backtest(df_5m, df_15m)
    assert len(trades_df) == 1
    trade = trades_df.iloc[0]
    assert trade["stop_after_tp1"] == trade["entry_price"]


def test_daily_limits_stop_trading():
    config = StrategyConfig(max_trades_per_day=2, daily_loss_limit_R=2.0)
    strategy = PutBuyStrategy(config)
    date_str = "2025-01-04"
    times = ["09:35", "09:40", "09:45", "09:50", "09:55", "10:00", "10:05"]
    rsi_seq = [46, 44, 46, 44, 46, 44, 46]
    macd_seq = [-0.05, -0.10, -0.05, -0.10, -0.05, -0.10, -0.05]
    rows = []
    for idx, t in enumerate(times):
        is_entry_bar = idx in {2, 4, 6}
        high = 107 if is_entry_bar else 105
        low = 99 if is_entry_bar else 100
        rows.append(
            _row(
                _ts(date_str, t),
                100,
                high,
                low,
                100,
                101,
                100,
                rsi_seq[idx],
                macd_seq[idx],
                10,
            )
        )
    df_5m = pd.DataFrame(rows)
    df_15m = _bear_15m_df(date_str, bearish=True)
    trades_df, _ = strategy.run_backtest(df_5m, df_15m)
    assert len(trades_df) == 2
