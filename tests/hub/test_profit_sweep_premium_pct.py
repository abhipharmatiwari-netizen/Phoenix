"""Tests for premium_pct target mode in profit sweep."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config.settings import Settings
from app.core.identifiers import BrokerAccountId, TenantId
from app.hub.exit_engines import ProfitSweepEngine
from app.pnl.profit_lock import ProfitLockManager
from app.pnl.pnl_engine import PnLEngine
from app.pnl.state_store import InMemoryPnLStateStore


def make_mock_settings(target_mode="absolute", daily_target=5000.0, premium_pct=0.5):
    settings = MagicMock(spec=Settings)
    settings.default_time_zone = "Asia/Kolkata"
    settings.profit_sweep_enabled = True
    settings.profit_sweep_strategy = "SIMPLE"
    settings.profit_simple_enabled = True
    settings.profit_daily_target = daily_target
    settings.profit_daily_target_paper = None
    settings.profit_multiple_enabled = False
    settings.profit_multiple_target = None
    settings.profit_sweep_cooldown_minutes = 30
    settings.profit_max_sweeps_per_day = 1
    settings.profit_lock_enabled = False
    settings.profit_lock_giveback_pct = 0.1
    settings.profit_lock_exit_cooldown_seconds = 0.0
    settings.profit_lock_require_signal = False
    settings.profit_simple_target_mode = target_mode
    settings.profit_simple_target_premium_pct = premium_pct
    return settings


def make_sweep_engine(settings, pnl_engine):
    from app.hub.sweep_state import SweepStateManager, SweepStateStore
    mock_sweep = MagicMock()
    mock_state_store = MagicMock()
    mock_state_store.get_positions = MagicMock(return_value=[MagicMock()])
    mock_state_store.get_strategy_signal_valid = MagicMock(return_value=None)
    mock_order_router = MagicMock()
    mock_order_router.submit_order = AsyncMock(
        return_value=("order_1", MagicMock(status="PENDING"))
    )
    fs = MagicMock(spec=SweepStateStore)
    fs.can_sweep_now = MagicMock(return_value=(True, None))
    fs.record_sweep = MagicMock()
    fs.get_sweep_state = MagicMock(
        return_value=MagicMock(sweep_count_today=0, last_sweep_time=None, last_reset_date="2026-01-15")
    )
    fs.get_minutes_since_last_sweep = MagicMock(return_value=None)
    mgr = SweepStateManager(state_store=fs)
    plm = ProfitLockManager(default_time_zone="Asia/Kolkata")
    engine = ProfitSweepEngine(
        settings=settings,
        pnl_engine=pnl_engine,
        sweep_engine=mock_sweep,
        state_store=mock_state_store,
        order_router=mock_order_router,
        sweep_state_manager=mgr,
        profit_lock_manager=plm,
    )
    engine._is_excluded_exchange = MagicMock(return_value=False)
    engine._exit_all_positions = AsyncMock(return_value=1)
    return engine, fs


def make_runner():
    runner = MagicMock()
    runner.is_running = True
    runner.tenant_id = TenantId("t1")
    runner.broker_account_id = BrokerAccountId("A1")
    runner.runtime_mode = "LIVE"
    return runner


@pytest.mark.asyncio
async def test_premium_pct_mode_arms_sweep_when_target_crossed():
    """
    premium_pct=0.25, open_premium=11765 → target=2941.25
    control_pnl=3042 → exceeds target → sweep fires.
    """
    store = InMemoryPnLStateStore()
    engine_pnl = PnLEngine(state_store=store)

    from app.pnl.types import TradeOpenEvent
    from datetime import datetime, timezone
    ev = TradeOpenEvent(
        tenant_id="t1", broker_account_id="A1", strategy_id="strat1",
        symbol="NIFTY28APR2624300CE", qty=65, entry_price=181.0, lot_size=1,
        trade_time=datetime.now(timezone.utc),
    )
    engine_pnl.on_open_position(ev)
    engine_pnl.update_control_open_pnl("t1", "A1", "strat1", current_ltp=134.0)

    settings = make_mock_settings(
        target_mode="premium_pct",
        daily_target=None,
        premium_pct=0.25,
    )
    sweep_engine, fs = make_sweep_engine(settings, engine_pnl)
    runner = make_runner()

    await sweep_engine.maybe_sweep_simple([runner])

    # open_premium = 65 * 181 = 11765; target = 11765 * 0.25 = 2941.25
    # control_pnl = (181 - 134) * 65 = 3055 > 2941.25 → fires
    fs.record_sweep.assert_called_once()


@pytest.mark.asyncio
async def test_absolute_mode_does_not_arm_when_control_pnl_below_5000():
    """
    absolute target=5000, control_pnl=3042 → does NOT arm sweep.
    """
    store = InMemoryPnLStateStore()
    engine_pnl = PnLEngine(state_store=store)

    from app.pnl.types import TradeOpenEvent
    from datetime import datetime, timezone
    ev = TradeOpenEvent(
        tenant_id="t1", broker_account_id="A1", strategy_id="strat1",
        symbol="NIFTY28APR2624300CE", qty=65, entry_price=181.0, lot_size=1,
        trade_time=datetime.now(timezone.utc),
    )
    engine_pnl.on_open_position(ev)
    engine_pnl.update_control_open_pnl("t1", "A1", "strat1", current_ltp=134.0)

    settings = make_mock_settings(target_mode="absolute", daily_target=5000.0)
    sweep_engine, fs = make_sweep_engine(settings, engine_pnl)
    runner = make_runner()

    await sweep_engine.maybe_sweep_simple([runner])

    # control_pnl ≈ 3055 < 5000 → NOT armed
    fs.record_sweep.assert_not_called()


@pytest.mark.asyncio
async def test_premium_pct_skips_when_no_open_premium():
    """When no open premium is tracked, premium_pct mode skips (no false triggers)."""
    store = InMemoryPnLStateStore()
    engine_pnl = PnLEngine(state_store=store)
    # No on_open_position called → control_open_premium = 0

    settings = make_mock_settings(target_mode="premium_pct", daily_target=None, premium_pct=0.25)
    sweep_engine, fs = make_sweep_engine(settings, engine_pnl)
    runner = make_runner()

    await sweep_engine.maybe_sweep_simple([runner])

    fs.record_sweep.assert_not_called()
