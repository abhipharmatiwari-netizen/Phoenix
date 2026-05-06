"""Tests verifying sweep uses control PnL when available, cash PnL as fallback."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config.settings import Settings
from app.core.identifiers import BrokerAccountId, TenantId
from app.hub.exit_engines import ProfitSweepEngine
from app.pnl.profit_lock import ProfitLockManager


@pytest.fixture
def mock_settings():
    settings = MagicMock(spec=Settings)
    settings.default_time_zone = "Asia/Kolkata"
    settings.profit_sweep_enabled = True
    settings.profit_sweep_strategy = "SIMPLE"
    settings.profit_simple_enabled = True
    settings.profit_daily_target = 5000.0
    settings.profit_daily_target_paper = None
    settings.profit_multiple_enabled = False
    settings.profit_multiple_target = None
    settings.profit_sweep_cooldown_minutes = 30
    settings.profit_max_sweeps_per_day = 1
    settings.profit_lock_enabled = False
    settings.profit_lock_giveback_pct = 0.1
    settings.profit_lock_exit_cooldown_seconds = 0.0
    settings.profit_lock_require_signal = False
    settings.profit_simple_target_mode = "absolute"
    settings.profit_simple_target_premium_pct = 0.5
    return settings


@pytest.fixture
def make_sweep_engine(mock_settings):
    def _factory(pnl_engine):
        from app.hub.sweep_state import SweepStateManager, SweepStateStore
        mock_sweep = MagicMock()
        mock_state_store = MagicMock()
        mock_state_store.get_positions = MagicMock(return_value=[])
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
            settings=mock_settings,
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
    return _factory


def make_runner(tenant_id="t1", account_id="A1"):
    runner = MagicMock()
    runner.is_running = True
    runner.tenant_id = TenantId(tenant_id)
    runner.broker_account_id = BrokerAccountId(account_id)
    runner.runtime_mode = "LIVE"
    return runner


@pytest.mark.asyncio
async def test_sweep_uses_control_pnl_when_available(make_sweep_engine):
    """When control PnL > target, sweep fires even if cash PnL < target."""
    pnl_engine = MagicMock()
    # Cash PnL is low (entry premium distorts it)
    pnl_engine.get_current_total_pnl = MagicMock(return_value=11765.0)
    # Control PnL reflects true economic profit = 6000 > target 5000
    pnl_engine.get_control_total_pnl = MagicMock(return_value=6000.0)

    engine, fs = make_sweep_engine(pnl_engine)
    runner = make_runner()

    await engine.maybe_sweep_simple([runner])

    # Control PnL was used and exceeded target → sweep should execute
    fs.record_sweep.assert_called_once()


@pytest.mark.asyncio
async def test_sweep_falls_back_to_cash_pnl_when_no_control_data(make_sweep_engine):
    """When get_control_total_pnl returns None, sweep falls back to cash PnL."""
    pnl_engine = MagicMock()
    pnl_engine.get_current_total_pnl = MagicMock(return_value=6000.0)
    pnl_engine.get_control_total_pnl = MagicMock(return_value=None)

    engine, fs = make_sweep_engine(pnl_engine)
    runner = make_runner()

    await engine.maybe_sweep_simple([runner])

    # Fallback to cash PnL (6000 > 5000 target) → sweep fires
    fs.record_sweep.assert_called_once()


@pytest.mark.asyncio
async def test_sweep_does_not_fire_when_control_pnl_below_target(make_sweep_engine):
    """When control PnL is below target, sweep does NOT fire."""
    pnl_engine = MagicMock()
    pnl_engine.get_current_total_pnl = MagicMock(return_value=11765.0)
    # Control PnL only 3042 < 5000 target → no sweep
    pnl_engine.get_control_total_pnl = MagicMock(return_value=3042.0)

    engine, fs = make_sweep_engine(pnl_engine)
    runner = make_runner()

    await engine.maybe_sweep_simple([runner])

    fs.record_sweep.assert_not_called()
