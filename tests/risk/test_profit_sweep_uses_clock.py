from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.clock import SimulatedClock
from app.hub.exit_engines import ProfitSweepEngine
from app.pnl.profit_lock import ProfitLockManager


def test_profit_sweep_today_uses_injected_clock():
    clock = SimulatedClock(datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc))
    settings = SimpleNamespace(
        default_time_zone="Asia/Kolkata",
        enable_profit_checks=True,
        profit_enable_daily_target=True,
        profit_daily_target=100.0,
        profit_daily_target_paper=100.0,
        profit_sweep_enabled=False,
        profit_sweep_strategy="SIMPLE",
    )
    engine = ProfitSweepEngine(
        settings=settings,
        pnl_engine=MagicMock(),
        sweep_engine=MagicMock(),
        state_store=MagicMock(),
        order_router=MagicMock(),
        sweep_state_manager=MagicMock(),
        profit_lock_manager=ProfitLockManager(default_time_zone="Asia/Kolkata"),
        clock=clock,
    )

    assert engine._today().isoformat() == "2026-03-02"


def test_profit_sweep_has_excluded_exchange_filter():
    settings = SimpleNamespace(
        default_time_zone="Asia/Kolkata",
        enable_profit_checks=True,
        profit_enable_daily_target=True,
        profit_daily_target=100.0,
        profit_daily_target_paper=100.0,
        profit_sweep_enabled=False,
        profit_sweep_strategy="SIMPLE",
        eod_exit_excluded_exchanges="MCX, CDS",
    )
    engine = ProfitSweepEngine(
        settings=settings,
        pnl_engine=MagicMock(),
        sweep_engine=MagicMock(),
        state_store=MagicMock(),
        order_router=MagicMock(),
        sweep_state_manager=MagicMock(),
        profit_lock_manager=ProfitLockManager(default_time_zone="Asia/Kolkata"),
    )

    assert engine._is_excluded_exchange("MCXFO") is True
    assert engine._is_excluded_exchange("NFO") is False
