import datetime as dt
import importlib
from types import SimpleNamespace

import pytest
from app.core.maintenance import MaintenanceManager


@pytest.fixture
def risk_manager(tmp_path, dummy_instrument_meta):
    mod = importlib.import_module("app.core.risk_manager")
    RiskManager = mod.RiskManager

    rm = RiskManager(
        instrument_meta=dummy_instrument_meta,
        order_client=SimpleNamespace(),
        risk_limits={
            "default": {
                "max_lots_per_trade": 5,
                "max_open_lots": 10,
                "max_notional_per_underlying": 1_000_000,
            },
            "per_underlying": {
                "NIFTY": {"max_open_lots": 20},
            },
        },
        state_path=str(tmp_path / "risk_state.json"),
    )
    return rm


class _PendingFillOrderClient:
    def __init__(self, row):
        self._row = dict(row)

    def get_order_book(self):
        return {"data": [{"orderid": "OID-1", **self._row}]}

    def get_order_details(self, order_id):
        assert order_id == "OID-1"
        return {"data": dict(self._row)}

    def _normalize_order_status(self, value):
        return str(value or "").strip().upper()


class _CancelableOrderClient:
    def __init__(self):
        self.cancel_calls = []

    def cancel_order(self, order_id, *, symbol=None, variety="NORMAL"):
        self.cancel_calls.append((order_id, symbol, variety))
        return {"status": True, "data": {"orderid": order_id}}

    def _normalize_order_status(self, value):
        return str(value or "").strip().upper()


def test_get_limits_for_underlying_default(risk_manager):
    limits = risk_manager._get_limits_for_underlying(None)
    assert limits["max_lots_per_trade"] == 5
    assert limits["max_open_lots"] == 10
    assert limits["max_notional_per_underlying"] == 1_000_000


def test_get_limits_for_underlying_override(risk_manager):
    limits = risk_manager._get_limits_for_underlying("nifty")
    assert limits["max_open_lots"] == 20


def test_get_current_open_lots(risk_manager):
    risk_manager.open_positions = {
        "NIFTY24MAYFUT": {"qty": 2},
        "BANKNIFTY24MAYFUT": {"qty": 2},
    }
    nifty_lots = risk_manager._get_current_open_lots("NIFTY")
    banknifty_lots = risk_manager._get_current_open_lots("BANKNIFTY")
    assert nifty_lots == 2
    assert banknifty_lots == 2
    assert risk_manager._get_current_open_lots("UNKNOWN") == 0


def test_reset_daily_if_new_day(risk_manager):
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    yesterday = dt.datetime.now(ist) - dt.timedelta(days=1)
    today = dt.datetime.now(ist)

    risk_manager.kill_switch_date = yesterday.date()
    risk_manager.daily_realized_pnl = 123.45
    risk_manager.kill_switch_activated = True

    risk_manager._reset_daily_if_new_day(now=today)

    assert risk_manager.kill_switch_date == today.date()
    assert risk_manager.daily_realized_pnl == 0
    assert risk_manager.kill_switch_activated is False


def test_square_off_all_prefers_hub_exit_submitter(risk_manager, monkeypatch):
    risk_manager.open_positions = {
        "NIFTY24MAYFUT": {
            "side": "SELL",
            "qty": 2,
            "entry_price": 100.0,
            "exchange": "NSE",
            "symboltoken": "26000",
            "tradingsymbol": "NIFTY24MAYFUT",
            "strategy_name": "option_strategy",
            "underlying": "NIFTY",
        }
    }
    hub_calls = []

    def _hub_submitter(**kwargs):
        hub_calls.append(kwargs)
        return True

    risk_manager.set_hub_exit_submitter(_hub_submitter)

    def _fail_direct_submit(**kwargs):
        raise AssertionError("Direct broker submit should not be used when hub exit submitter succeeds")

    monkeypatch.setattr(risk_manager, "_submit_order", _fail_direct_submit)

    risk_manager.square_off_all(trade_mode="LIVE", tag="MAX_LOSS_EXIT")

    assert len(hub_calls) == 1
    assert hub_calls[0]["reason"] == "MAX_LOSS_EXIT"
    assert "NIFTY24MAYFUT" not in risk_manager.open_positions
    assert risk_manager.should_suppress_broker_sync_entry("NIFTY24MAYFUT") is True


def test_square_off_all_falls_back_to_direct_submit_when_hub_submitter_returns_false(
    risk_manager, monkeypatch
):
    risk_manager.open_positions = {
        "NIFTY24MAYFUT": {
            "side": "SELL",
            "qty": 1,
            "entry_price": 101.0,
            "exchange": "NSE",
            "symboltoken": "26000",
            "tradingsymbol": "NIFTY24MAYFUT",
            "strategy_name": "option_strategy",
            "underlying": "NIFTY",
        }
    }
    hub_calls = []
    direct_calls = []

    def _hub_submitter(**kwargs):
        hub_calls.append(kwargs)
        return False

    def _direct_submit(**kwargs):
        direct_calls.append(kwargs)
        return True, "COMPLETE", "OID-1", None

    risk_manager.set_hub_exit_submitter(_hub_submitter)
    monkeypatch.setattr(risk_manager, "_submit_order", _direct_submit)

    risk_manager.square_off_all(trade_mode="LIVE", tag="MAX_LOSS_EXIT")

    assert len(hub_calls) == 1
    assert len(direct_calls) == 1
    assert "NIFTY24MAYFUT" not in risk_manager.open_positions


def test_square_off_all_cancels_pending_closing_orders_before_exit_submission(
    risk_manager, monkeypatch
):
    risk_manager.order_client = _CancelableOrderClient()
    risk_manager.open_positions = {
        "NIFTY24MAYFUT": {
            "side": "SELL",
            "qty": 1,
            "entry_price": 101.0,
            "exchange": "NSE",
            "symboltoken": "26000",
            "tradingsymbol": "NIFTY24MAYFUT",
            "strategy_name": "option_strategy",
            "underlying": "NIFTY",
        }
    }
    risk_manager._record_pending_order(
        order_id="OID-TGT-1",
        ctx={
            "label": "NIFTY24MAYFUT",
            "side": "BUY",
            "qty": 1,
            "price": 98.4,
            "template_name": "ema20-trend",
            "role": None,
            "underlying": "NIFTY",
            "strategy_name": "ema20-trend",
            "reason": "TARGET",
            "count_for_spreads": False,
            "is_closing": True,
            "exchange": "NSE",
            "symboltoken": "26000",
            "tradingsymbol": "NIFTY24MAYFUT",
            "tag": "TARGET_ema20-trend",
            "product_type": "INTRADAY",
            "lot_size": 1,
        },
    )
    direct_calls = []

    def _direct_submit(**kwargs):
        direct_calls.append(kwargs)
        return True, "COMPLETE", "OID-EXIT-1", None

    monkeypatch.setattr(risk_manager, "_submit_order", _direct_submit)

    risk_manager.square_off_all(trade_mode="LIVE", tag="MAX_LOSS_EXIT")

    assert risk_manager.order_client.cancel_calls == [
        ("OID-TGT-1", "NIFTY24MAYFUT", "NORMAL")
    ]
    assert "OID-TGT-1" not in risk_manager._pending_orders
    assert len(direct_calls) == 1
    assert "NIFTY24MAYFUT" not in risk_manager.open_positions


def test_square_off_eod_can_close_via_hub_exit_submitter(risk_manager, monkeypatch):
    risk_manager.open_positions = {
        "NIFTY24MAYFUT": {
            "side": "SELL",
            "qty": 1,
            "entry_price": 102.0,
            "exchange": "NSE",
            "symboltoken": "26000",
            "tradingsymbol": "NIFTY24MAYFUT",
            "strategy_name": "option_strategy",
            "underlying": "NIFTY",
        }
    }
    hub_calls = []

    def _hub_submitter(**kwargs):
        hub_calls.append(kwargs)
        return True

    risk_manager.set_hub_exit_submitter(_hub_submitter)

    def _fail_direct_submit(**kwargs):
        raise AssertionError("Direct broker submit should not be used when hub exit submitter succeeds")

    monkeypatch.setattr(risk_manager, "_submit_order", _fail_direct_submit)

    closed_underlyings = risk_manager.square_off_eod(
        trade_mode="LIVE",
        tag="EOD_SQUARE_OFF",
    )

    assert len(hub_calls) == 1
    assert closed_underlyings == {"NIFTY"}
    assert "NIFTY24MAYFUT" not in risk_manager.open_positions


def test_register_exit_uses_position_lot_size_when_meta_is_missing(risk_manager, monkeypatch):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.dashboard_bus, "add_position", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod.dashboard_bus, "close_position", lambda *args, **kwargs: None)

    risk_manager.instrument_meta = {
        "NG_ATM_CE_310": {
            "symbol": "NATURALGAS24MAR26310CE",
            "exchange": "MCX",
            "token": "505131",
            "lot_size": 1250,
        }
    }

    risk_manager._register_entry(
        label="NG_ATM_CE_310",
        side="SELL",
        qty=1,
        price=26.6,
        template_name="BROKER_SYNC",
        role=None,
        underlying="NG",
        strategy_name="BROKER_SYNC",
        reason="BROKER_SYNC",
        ml_info=None,
    )

    risk_manager.instrument_meta = {}
    risk_manager._register_exit("NG_ATM_CE_310", 27.75, 1)

    assert risk_manager.realized_pnl == pytest.approx(-1437.5)


def test_reconcile_pending_closing_order_uses_fill_price_in_lots(risk_manager, monkeypatch):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.dashboard_bus, "add_position", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod.dashboard_bus, "close_position", lambda *args, **kwargs: None)

    risk_manager.instrument_meta = {
        "NIFTY_ATM_CE_23150": {
            "symbol": "NIFTY17MAR2623150CE",
            "exchange": "NFO",
            "token": "126001",
            "lot_size": 65,
            "underlying": "NIFTY",
        }
    }
    risk_manager.order_client = _PendingFillOrderClient(
        {
            "orderstatus": "complete",
            "averageprice": "201.20",
            "filledshares": "65",
            "price": "198.40",
        }
    )

    risk_manager._register_entry(
        label="NIFTY_ATM_CE_23150",
        side="BUY",
        qty=1,
        price=197.40,
        template_name="ema20-trend",
        role=None,
        underlying="NIFTY",
        strategy_name="ema20-trend",
        reason="ENTRY",
        ml_info=None,
        strategy_context={"managed_exit_mode": "strategy"},
    )
    risk_manager._record_pending_order(
        order_id="OID-1",
        ctx={
            "label": "NIFTY_ATM_CE_23150",
            "side": "SELL",
            "qty": 1,
            "price": 198.40,
            "template_name": "ema20-trend",
            "role": None,
            "underlying": "NIFTY",
            "strategy_name": "ema20-trend",
            "reason": "TARGET",
            "count_for_spreads": False,
            "is_closing": True,
            "exchange": "NFO",
            "symboltoken": "126001",
            "tradingsymbol": "NIFTY17MAR2623150CE",
            "product_type": "INTRADAY",
            "lot_size": 65,
        },
    )

    outcome = risk_manager.reconcile_pending_closing_orders_for_label("NIFTY_ATM_CE_23150")

    assert outcome["closed"] is True
    assert "NIFTY_ATM_CE_23150" not in risk_manager.open_positions
    assert risk_manager._pending_orders == {}
    assert risk_manager.realized_pnl == pytest.approx(247.0)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_maintenance_block_rejects_new_entries_for_all_entry_sides(risk_manager, side):
    maintenance = MaintenanceManager()
    maintenance.block_new_entries("NIFTY", reason="indicator_gap_data_stale:300")
    risk_manager.attach_maintenance(maintenance)

    decision = risk_manager.place_order(
        label="NIFTY24MAYFUT",
        side=side,
        qty=1,
        price=100.0,
        template_name="TEST_ENTRY",
        strategy_name="test_strategy",
    )

    assert decision.allowed is False
    assert decision.reason == "maintenance_block"
